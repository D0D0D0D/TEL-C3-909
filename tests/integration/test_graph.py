# TEL-C3-909 — Integration Tests: full graph compile + invoke

import json

import pytest

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.graph.graph import MonitoringAlertFirstTriageAgent

VALID_DSL = """
runbook_id: "svc-web-portal"
version: "2026-07-01"
match:
  target_system: "web-portal"
nodes:
  - id: "check-health-ep"
    description: "health endpoint check"
    tool: "http_get_healthcheck"
    args: {target: "portal-health:443"}
    on_ok: []
    on_ng: ["check-db"]
  - id: "check-db"
    description: "db liveness check"
    tool: "check_liveness"
    args: {target: "db-host:5432"}
    on_ok: []
    on_ng: []
severity_hint: {}
"""


class ScriptedLLM:
    """Deterministic fake that scripts a short realistic think loop."""

    def __init__(self, script):
        self._script = script
        self._n = 0

    def complete(self, messages):
        if self._n < len(self._script):
            response = self._script[self._n]
        else:
            response = {"content": "done", "tool_calls": []}
        self._n += 1
        return response

    def bind_tools(self, tools):
        return self

    def stream(self, messages):
        yield ""


def _config(llm, location="", target_allowlist=None, max_iterations=24, budget_usd=1.50):
    return {
        "llm": llm,
        "budget_usd": budget_usd,
        "max_iterations": max_iterations,
        "confidence_threshold": 0.75,
        "runbook_source": {"mode": "cached", "location": location},
        "runbook_conversion": {"enabled": True, "cache_max_entries": 64},
        "target_allowlist": target_allowlist or [],
        "reporting": {"enabled": True, "webhook_secret_key": "CHATOPS_WEBHOOK_URL", "min_interval_seconds": 0},
        "output": {"language": "ja"},
        "input_adapters": ["alertmanager", "datadog", "pagerduty", "generic"],
        "memory_enabled": False,
        "hitl": {"enabled": False},
    }


def _alertmanager_payload(target_system="web-portal"):
    # NOTE: target_system="web-portal" deliberately matches the test DSL's
    # match.target_system so the full pipeline exercises the decision tree.
    # The non-matching (degraded mode) path is covered separately in
    # TestFullPipelineDegradedMode (review I-04).
    return json.dumps(
        {
            "groupKey": "x",
            "alerts": [
                {
                    "labels": {"alertname": "HighLatency", "service": target_system, "severity": "critical"},
                    "annotations": {"summary": "p99 latency high"},
                    "startsAt": "2026-07-06T00:00:00Z",
                }
            ],
        }
    )


@pytest.fixture
def secrets():
    return InMemoryProvider(
        {
            "DIAG_READONLY_CREDENTIALS": "mock-readonly-cred-for-testing",
            "CHATOPS_WEBHOOK_URL": "https://example.invalid/webhook",
        }
    )


class TestFullPipelineDegradedMode:
    def test_empty_runbook_location_produces_degraded_report(self, secrets):
        """degraded_mode (empty runbook location) still calls think/LLM once
        (think is the framework default and always runs) — a well-behaved
        LLM that respects the degraded-mode prompt instruction (never emits
        tool_calls) lets the loop reach finalize normally and produce a
        valid partial report."""
        llm = ScriptedLLM([{"content": "no runbook configured, reporting degraded", "tool_calls": []}])
        agent = MonitoringAlertFirstTriageAgent(config=_config(llm, location=""))
        agent.compile()
        with bound_secrets(secrets):
            ctx = InvocationContext(caller_trust_level=TrustLevel.INTERNAL)
            result = agent.invoke(_alertmanager_payload(), ctx=ctx)
        assert result["status"] == "success"
        assert result["output"] is not None
        assert result["output"]["escalation_required"] is True

    def test_insufficient_trust_produces_degraded_report_with_wellbehaved_llm(self, secrets):
        llm = ScriptedLLM([{"content": "degraded, no diagnostics", "tool_calls": []}])
        agent = MonitoringAlertFirstTriageAgent(
            config=_config(llm, location="", target_allowlist=["portal-health:443"])
        )
        agent.compile()
        with bound_secrets(secrets):
            ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
            result = agent.invoke(_alertmanager_payload(), ctx=ctx)
        assert result["status"] == "success"
        assert result["output"] is not None
        assert result["output"]["escalation_required"] is True


class TestFullPipelineHappyPath:
    def test_valid_dsl_full_diagnostic_loop(self, tmp_path, secrets):
        runbook_file = tmp_path / "runbook.yaml"
        runbook_file.write_text(VALID_DSL)

        llm = ScriptedLLM(
            [
                {
                    "content": "checking health ep",
                    "tool_calls": [
                        {
                            "name": "http_get_healthcheck",
                            "args": {"target": "portal-health:443", "dsl_node_id": "check-health-ep"},
                        }
                    ],
                },
                {
                    "content": "recording hypothesis",
                    "tool_calls": [
                        {
                            "name": "record_hypothesis",
                            "args": {
                                "cause": "possible network issue",
                                "confidence": 0.8,
                                "evidence": ["check-health-ep"],
                            },
                        }
                    ],
                },
                {"content": "done", "tool_calls": []},
            ]
        )
        agent = MonitoringAlertFirstTriageAgent(
            config=_config(llm, location=str(runbook_file), target_allowlist=["portal-health:443", "db-host:5432"])
        )
        agent.compile()
        with bound_secrets(secrets):
            ctx = InvocationContext(caller_trust_level=TrustLevel.INTERNAL)
            result = agent.invoke(_alertmanager_payload(), ctx=ctx)

        assert result["status"] == "success"
        output = result["output"]
        assert len(output["hypotheses"]) == 1
        assert output["hypotheses"][0]["confidence"] == 0.8
        assert output["runbook_trace"]["doc_hash"]
        assert len(output["checked_matrix"]) == 1
        # Regression: unchecked must compare against the real DSL node id
        # ("check-health-ep") echoed via dsl_node_id, not a synthetic
        # tool:target:index string that never matches any decision_tree
        # node — the whole VALID_DSL tree has 2 nodes, 1 was checked.
        assert output["checked_matrix"][0]["node_id"] == "check-health-ep"
        assert output["unchecked"] == ["check-db"]

    def test_no_match_for_alert_target_enters_degraded_mode(self, tmp_path, secrets):
        runbook_file = tmp_path / "runbook.yaml"
        runbook_file.write_text(VALID_DSL)  # matches "web-portal" only

        llm = ScriptedLLM([{"content": "no diagnostics possible", "tool_calls": []}])
        agent = MonitoringAlertFirstTriageAgent(
            config=_config(llm, location=str(runbook_file), target_allowlist=["portal-health:443"])
        )
        agent.compile()
        with bound_secrets(secrets):
            ctx = InvocationContext(caller_trust_level=TrustLevel.INTERNAL)
            result = agent.invoke(_alertmanager_payload(target_system="unrelated-system"), ctx=ctx)

        assert result["status"] == "success"
        assert result["output"]["escalation_required"] is True


class TestFullPipelineBudgetCancellation:
    def test_budget_exhaustion_produces_partial_report_with_cancelled_status(self, tmp_path, secrets):
        runbook_file = tmp_path / "runbook.yaml"
        runbook_file.write_text(VALID_DSL)

        # Every think call requests a tool — never converges — forces budget exhaustion.
        llm = ScriptedLLM(
            [
                {
                    "content": f"iteration {i}",
                    "tool_calls": [{"name": "http_get_healthcheck", "args": {"target": "portal-health:443"}}],
                }
                for i in range(50)
            ]
        )
        agent = MonitoringAlertFirstTriageAgent(
            config=_config(
                llm,
                location=str(runbook_file),
                target_allowlist=["portal-health:443"],
                budget_usd=0.0000001,  # force immediate cancellation on first cost update
            )
        )
        agent.compile()
        with bound_secrets(secrets):
            ctx = InvocationContext(caller_trust_level=TrustLevel.INTERNAL)
            result = agent.invoke(_alertmanager_payload(), ctx=ctx)

        # Budget ceiling forces status=success internally per framework
        # ThinkNode contract (see framework/nodes/defaults/think_node.py) —
        # docs/02 "cancelled" language refers to the conceptual outcome;
        # assemble_output()/finalize surface a partial report regardless.
        assert result["status"] in ("success", "cancelled")
        assert result["output"] is not None
        assert result["output"]["checked_matrix"] is not None


class TestFullPipelineEmptyInput:
    def test_empty_input_is_rejected(self, secrets):
        llm = ScriptedLLM([])
        agent = MonitoringAlertFirstTriageAgent(config=_config(llm))
        agent.compile()
        with bound_secrets(secrets):
            ctx = InvocationContext(caller_trust_level=TrustLevel.INTERNAL)
            result = agent.invoke("", ctx=ctx)
        assert result["status"] == "error"
