# TC-08 family — S-1 caller contract (docs/02_design.md §Security Model S-1):
# "the standard caller is the monitoring webhook receiver, which invokes
# with caller_trust_level=INTERNAL. VERIFIED_EXTERNAL invocations still
# work — initialize/think execute — but full diagnostic execution (act) is
# refused; finalize still produces a valid degraded/no-diagnostics report
# rather than crashing or returning a null/error-only result."

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.graph.graph import MonitoringAlertFirstTriageAgent

VALID_DSL = """
runbook_id: "svc-web-portal"
version: "1"
match:
  target_system: "web-portal"
nodes:
  - id: "check-health-ep"
    description: "health endpoint check"
    tool: "http_get_healthcheck"
    args: {target: "portal-health:443"}
    on_ok: []
    on_ng: []
severity_hint: {}
"""


class WellBehavedLLM:
    """Simulates an LLM that respects the degraded-mode prompt instruction
    (never emits tool_calls when told diagnostics are unavailable)."""

    def complete(self, messages):
        return {"content": "trust level insufficient, reporting degraded", "tool_calls": []}

    def bind_tools(self, tools):
        return self

    def stream(self, messages):
        yield ""


def _config(llm, location):
    return {
        "llm": llm,
        "budget_usd": 1.50,
        "max_iterations": 24,
        "confidence_threshold": 0.75,
        "runbook_source": {"mode": "cached", "location": location},
        "runbook_conversion": {"enabled": True, "cache_max_entries": 64},
        "target_allowlist": ["portal-health:443"],
        "reporting": {"enabled": True, "webhook_secret_key": "CHATOPS_WEBHOOK_URL", "min_interval_seconds": 0},
        "output": {"language": "ja"},
        "input_adapters": ["alertmanager", "datadog", "pagerduty", "generic"],
        "memory_enabled": False,
        "hitl": {"enabled": False},
    }


class TestTC08TrustLevelCallerContract:
    def test_internal_caller_gets_full_diagnostic_execution(self, tmp_path):
        runbook_file = tmp_path / "runbook.yaml"
        runbook_file.write_text(VALID_DSL)

        class ScriptedLLM:
            def __init__(self):
                self.n = 0

            def complete(self, messages):
                self.n += 1
                if self.n == 1:
                    return {
                        "content": "check",
                        "tool_calls": [{"name": "http_get_healthcheck", "args": {"target": "portal-health:443"}}],
                    }
                return {"content": "done", "tool_calls": []}

            def bind_tools(self, tools):
                return self

            def stream(self, messages):
                yield ""

        agent = MonitoringAlertFirstTriageAgent(config=_config(ScriptedLLM(), str(runbook_file)))
        agent.compile()

        secrets = InMemoryProvider(
            {"DIAG_READONLY_CREDENTIALS": "mock-readonly-cred-for-testing", "CHATOPS_WEBHOOK_URL": "https://example.invalid/webhook"}
        )
        payload = json.dumps({"alerts": [{"labels": {"alertname": "A", "service": "web-portal"}}]})

        with bound_secrets(secrets):
            ctx = InvocationContext(caller_trust_level=TrustLevel.INTERNAL)
            result = agent.invoke(payload, ctx=ctx)

        assert result["status"] == "success"
        assert len(result["output"]["checked_matrix"]) == 1

    def test_verified_external_caller_gets_degraded_report_not_crash(self, tmp_path):
        """The documented degraded behavior (docs/02): a VERIFIED_EXTERNAL
        caller with a well-behaved LLM still produces a valid, non-null
        finalize output reporting that diagnostics require the INTERNAL
        (webhook-receiver) path — not a crash, not an empty/None output."""
        runbook_file = tmp_path / "runbook.yaml"
        runbook_file.write_text(VALID_DSL)

        agent = MonitoringAlertFirstTriageAgent(config=_config(WellBehavedLLM(), str(runbook_file)))
        agent.compile()

        secrets = InMemoryProvider(
            {"DIAG_READONLY_CREDENTIALS": "mock-readonly-cred-for-testing", "CHATOPS_WEBHOOK_URL": "https://example.invalid/webhook"}
        )
        payload = json.dumps({"alerts": [{"labels": {"alertname": "A", "service": "web-portal"}}]})

        with bound_secrets(secrets):
            ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
            result = agent.invoke(payload, ctx=ctx)

        assert result["status"] == "success"
        assert result["output"] is not None
        assert result["output"]["escalation_required"] is True
        assert len(result["output"]["checked_matrix"]) == 0  # no diagnostics performed
