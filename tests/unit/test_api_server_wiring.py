# TEL-C3-909 — Entry-point adapter wiring.
#
# The auth core itself belongs to the entry-adapter part and is covered by
# tests/parts/entry-adapter/. What is tested here is THIS template's wiring of
# it, plus the standalone HTTP surface that no other test exercises.
#
# That gap is why this file exists: CI never executes start-up, so an
# import-time failure or an always-ANONYMOUS caller stays invisible behind
# green gates. Before this change
# `src.api.server` could not be imported under its own declared dependencies,
# and every HTTP /invoke landed on ANONYMOUS and was refused by the S-1 gate.

from __future__ import annotations

import importlib
import json
import sys

import pytest
from fastapi.testclient import TestClient

_ENV_KEYS = ("INVOKE_AUTH_TOKEN", "CI", "CI_JOB_NAME", "STG_MOCK_MODE")


def _reload_server(monkeypatch, **env):
    """Import src.api.server fresh under the given environment."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for name in [n for n in list(sys.modules) if n.startswith("src.api")]:
        del sys.modules[name]
    return importlib.import_module("src.api.server")


def _payload() -> dict:
    return {
        "input": json.dumps(
            {
                "groupKey": "test",
                "alerts": [
                    {
                        "labels": {"alertname": "HighLatency", "service": "web-portal",
                                   "severity": "critical"},
                        "annotations": {"summary": "p99 latency high"},
                        "startsAt": "2026-07-29T00:00:00Z",
                    }
                ],
            }
        ),
        "session_id": "test-session",
    }


@pytest.fixture
def server(monkeypatch):
    """Server wired as the deploy-stg job wires it: a bearer token, no secrets."""
    return _reload_server(monkeypatch, INVOKE_AUTH_TOKEN="test-token")


class TestStandaloneBearerAuth:
    def test_valid_bearer_is_promoted_and_succeeds(self, server):
        response = TestClient(server.app).post(
            "/invoke", json=_payload(), headers={"Authorization": "Bearer test-token"}
        )

        assert response.status_code == 200
        # Promotion to VERIFIED_EXTERNAL is what makes this succeed — on
        # ANONYMOUS the S-1 gate refuses and the agent returns status=error.
        assert response.json()["status"] == "success"

    def test_wrong_bearer_is_refused(self, server):
        response = TestClient(server.app).post(
            "/invoke", json=_payload(), headers={"Authorization": "Bearer wrong"}
        )

        assert response.status_code == 401

    def test_missing_bearer_is_refused(self, server):
        assert TestClient(server.app).post("/invoke", json=_payload()).status_code == 401

    def test_unauthenticated_caller_is_rejected_not_defaulted(self, monkeypatch):
        """With no token configured and no middleware the caller stays
        ANONYMOUS and must be REFUSED — not quietly defaulted to a verified
        trust. A default like that is a fail-open S-1 bypass."""
        server = _reload_server(monkeypatch)  # no INVOKE_AUTH_TOKEN

        response = TestClient(server.app).post("/invoke", json=_payload())

        assert response.status_code == 403

    def test_middleware_trust_is_used_as_is(self, server):
        """A verified middleware trust wins; the bearer path is not consulted."""
        from framework.schemas.trust_level import TrustLevel

        from src.entry_adapter.auth import _resolve_trust

        class _Req:
            class state:  # noqa: N801 - mimics starlette's request.state
                trust_level = TrustLevel.INTERNAL

            headers: dict = {}

        trust, token_authenticated = _resolve_trust(_Req())

        assert trust == TrustLevel.INTERNAL
        assert token_authenticated is False


class TestLlmSeamIsNotEnvironmentGated:
    """The stand-in is selected by "can a real client be built", never by
    environment variables.

    An earlier revision gated it on CI + CI_JOB_NAME + STG_MOCK_MODE. That is
    unsound in both directions — CI=true is set by every GitLab job including
    production deploys, and env vars are inherited — so these tests pin the
    absence of that coupling.
    """

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {"CI": "true", "CI_JOB_NAME": "deploy-stg", "STG_MOCK_MODE": "true"},
            {"CI": "true", "CI_JOB_NAME": "run-tests"},
            {"STG_MOCK_MODE": "true"},
        ],
        ids=["no-env", "full-stg-signals", "other-ci-job", "mock-mode-only"],
    )
    def test_wiring_does_not_change_with_ci_variables(self, monkeypatch, env):
        server = _reload_server(monkeypatch, INVOKE_AUTH_TOKEN="t", **env)

        from src.services.llm_client import OfflineTriageLLM

        # Same outcome in every environment: no real client is constructible
        # here, so the stand-in is used — and no CI variable changed that.
        assert isinstance(server.agent.config["llm"], OfflineTriageLLM)

    def test_a_real_client_is_preferred_when_constructible(self, monkeypatch):
        """When a credential is provisioned the real client is used — the
        stand-in never shadows a working production client."""
        from shared.services.llm.base_llm import BaseLLM

        class _RealClient(BaseLLM):
            """Stands in for the production Anthropic client."""

            def complete(self, messages):
                return {"content": "", "tool_calls": []}

            def bind_tools(self, tools):
                return self

            def stream(self, messages):
                yield ""

        real = _RealClient()
        server = _reload_server(monkeypatch, INVOKE_AUTH_TOKEN="t")
        monkeypatch.setattr(server, "build_llm", lambda _provider: real)

        rebuilt = server._build_agent()

        assert rebuilt.config["llm"] is real


class TestRuntimeConfigReachesTheGraph:
    """config/config.yaml must actually drive the deployed Graph.

    This adapter previously hardcoded runbook_source.location="" and
    target_allowlist=[], so following the deployment procedure in docs/07
    changed nothing: every run degraded to runbook_location_not_configured and
    no diagnostic was ever executed. The STG smoke check could not catch it
    either, because that degraded path still returns status=success.
    """

    def test_operator_tunable_values_are_read_from_runtime_config(self, server):
        import yaml

        with open("config/config.yaml", encoding="utf-8") as fh:
            declared = yaml.safe_load(fh)

        for key in ("budget_usd", "max_iterations", "confidence_threshold",
                    "runbook_source", "target_allowlist", "input_adapters"):
            assert server.agent.config[key] == declared[key], f"{key} did not reach the Graph"

    def test_a_configured_runbook_and_allowlist_reach_the_graph(self, monkeypatch, tmp_path):
        """The values operators actually change. Pinning the current (empty)
        values alone would pass even if the loader silently returned {}."""
        runtime_config = tmp_path / "config.yaml"
        runtime_config.write_text(
            "budget_usd: 2.0\n"
            "runbook_source:\n"
            "  mode: cached\n"
            "  location: /srv/runbooks/network.md\n"
            "target_allowlist:\n"
            "  - portal-health:443\n"
        )
        server = _reload_server(monkeypatch, INVOKE_AUTH_TOKEN="t")
        monkeypatch.setattr(server, "_CONFIG_YAML", runtime_config)

        rebuilt = server._build_agent()

        assert rebuilt.config["runbook_source"]["location"] == "/srv/runbooks/network.md"
        assert rebuilt.config["target_allowlist"] == ["portal-health:443"]
        assert rebuilt.config["budget_usd"] == 2.0

    def test_missing_runtime_config_fails_loudly_rather_than_defaulting(self, monkeypatch, tmp_path):
        """No runtime config must NOT mean "come up on built-in defaults".

        A missing config file is a deployment error and must not be converted
        into an empty dict or built-in defaults.
        """
        server = _reload_server(monkeypatch, INVOKE_AUTH_TOKEN="t")
        monkeypatch.setattr(server, "_CONFIG_YAML", tmp_path / "absent.yaml")

        with pytest.raises(FileNotFoundError):
            server._build_agent()


class TestStartupDegradesInsteadOfBlocking:
    """A broken environment must not make the module unimportable — that is a
    production start-up blocker every gate stays green through."""

    @pytest.fixture
    def broken_server(self, monkeypatch):
        server = _reload_server(monkeypatch, INVOKE_AUTH_TOKEN="t")
        # Reproduce import-time failure without depending on which dependency
        # happens to be missing in this environment.
        monkeypatch.setattr(server, "agent", None)
        monkeypatch.setattr(server, "_agent_init_error",
                            RuntimeError("simulated wiring failure"))
        return server

    def test_health_returns_503_so_the_load_balancer_drains(self, broken_server):
        """A 200 body would keep traffic flowing to a process whose every
        /invoke answers 503."""
        response = TestClient(broken_server.app).get("/health")

        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"

    def test_health_does_not_leak_the_failure_cause(self, broken_server):
        body = TestClient(broken_server.app).get("/health").json()

        assert "reason" not in body
        assert "simulated wiring failure" not in json.dumps(body)

    def test_livez_stays_200_so_restarts_are_not_triggered(self, broken_server):
        """Liveness is separate: restarting cannot conjure a missing secret."""
        response = TestClient(broken_server.app).get("/livez")

        assert response.status_code == 200
        assert response.json()["status"] == "alive"

    def test_invoke_returns_503_not_a_crash(self, broken_server):
        response = TestClient(broken_server.app).post("/invoke", json=_payload())

        assert response.status_code == 503
