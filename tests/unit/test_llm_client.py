# TEL-C3-909 — LLM seam.
#
# The stand-in is production code, not a test fixture: it is what keeps
# /invoke runnable without credentials (project pattern — Enterprise Deep Research & Long-Form Report Generation Agent
# ScriptedResearchLLM). Its selection rule carries the security weight, so
# that is what these pin: a missing credential degrades, but a PROVISIONED
# credential never silently falls back — a production process with a broken
# client must fail readiness, not serve empty triage while reporting healthy.

from __future__ import annotations

import pytest

from src.services.llm_client import OfflineTriageLLM, build_llm


class _NoCredentials:
    """Provider that reports the key is simply not configured here."""

    def require(self, key):
        from framework.secrets.base import MissingSecret

        raise MissingSecret(key)


class _BrokenProvider:
    """Provider that is itself failing — timeout, authorization failure,
    secret-store outage. NOT the same thing as "no key configured"."""

    def require(self, key):
        raise RuntimeError("secret store unreachable")


class _WithCredentials:
    def __init__(self, key="test-api-key"):
        self._key = key

    def require(self, key):
        return self._key


def _stub_anthropic_module(monkeypatch, factory):
    """Install a fake shared.services.llm.anthropic_client.

    The real module imports langchain_anthropic at module scope, and that
    package is not among this project's declared dependencies — so the real
    client path cannot be reached in this environment without substituting the
    module itself. Patching an attribute is not enough: the import inside
    build_llm() would still execute the real module body.
    """
    import sys
    import types

    module = types.ModuleType("shared.services.llm.anthropic_client")
    module.AnthropicClient = factory
    monkeypatch.setitem(sys.modules, "shared.services.llm.anthropic_client", module)


class TestOfflineStandIn:
    def test_emits_no_tool_calls(self):
        """No tool calls means ThinkNode finalizes on the first iteration and
        /invoke returns a well-formed degraded report."""
        result = OfflineTriageLLM().complete("any prompt")

        assert result["tool_calls"] == []
        assert result["content"]

    def test_does_not_synthesize_diagnostics(self):
        """It must not invent tool calls to look busy. record_hypothesis takes
        (cause, confidence, evidence) and does NOT advance `visited`, so a
        synthesized call would both break the contract and fail to converge."""
        for prompt in ("縮退モードです", "未確認ノード候補: ['check-health-ep']", ""):
            assert OfflineTriageLLM().complete(prompt)["tool_calls"] == []

    def test_is_stateless_across_calls(self):
        """One compiled agent serves many requests; a stand-in holding a cursor
        would leak one request's progress into the next."""
        llm = OfflineTriageLLM()

        assert llm.complete("p") == llm.complete("p")

    def test_bind_tools_returns_self_without_changing_behaviour(self):
        llm = OfflineTriageLLM()

        assert llm.bind_tools(["record_hypothesis"]) is llm
        assert llm.complete("p")["tool_calls"] == []


class TestSelectionIsOnTheCredential:
    def test_typed_absence_yields_the_stand_in(self):
        """MissingSecret is the provider's signal for "not configured here"."""
        assert isinstance(build_llm(_NoCredentials()), OfflineTriageLLM)

    def test_provider_outage_propagates_instead_of_degrading(self):
        """A failing secret store must NOT look like "no key configured".

        Degrading here would bring a production process up healthy on the
        stand-in, answering /invoke with empty triage at status=success — and
        deploy-stg's agent_status_success would pass too, certifying a broken
        deployment.
        """
        with pytest.raises(RuntimeError, match="secret store unreachable"):
            build_llm(_BrokenProvider())

    def test_typed_absence_never_raises(self):
        """Callers wire this straight into config["llm"] at import time; an
        exception here would be a start-up blocker."""
        assert build_llm(_NoCredentials()) is not None

    def test_provisioned_credential_builds_a_real_client(self, monkeypatch):
        captured = {}

        class _FakeAnthropic:
            def __init__(self, config):
                captured.update(config)

        _stub_anthropic_module(monkeypatch, _FakeAnthropic)

        client = build_llm(_WithCredentials("secret-key"))

        assert isinstance(client, _FakeAnthropic)
        assert not isinstance(client, OfflineTriageLLM)
        # config dict is mandatory for AnthropicClient — an argument-less call
        # raises TypeError, which an earlier revision swallowed into the
        # stand-in, silently disabling the real client in production.
        assert captured["api_key"] == "secret-key"

    def test_model_id_is_not_hardcoded(self, monkeypatch):
        """Project standard: the concrete model identity is an operational
        decision, so the template must not pin one."""
        captured = {}

        class _FakeAnthropic:
            def __init__(self, config):
                captured.update(config)

        _stub_anthropic_module(monkeypatch, _FakeAnthropic)
        build_llm(_WithCredentials())

        assert "model" not in captured

    def test_failure_after_a_credential_exists_propagates(self, monkeypatch):
        """The load-bearing asymmetry. Once a key is provisioned this is a real
        environment: a broken client must surface as a failed readiness probe,
        not as a stand-in quietly answering with empty triage."""
        def _explode(config):
            raise ValueError("simulated client construction failure")

        _stub_anthropic_module(monkeypatch, _explode)

        with pytest.raises(ValueError, match="simulated client construction failure"):
            build_llm(_WithCredentials())
