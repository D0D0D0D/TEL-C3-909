# PB-5: Checkpoint safety — dynamic verification (not just AST static scan).
#
# TEL-C3-909's core safety claim is that the read-only credential
# (DIAG_READONLY_CREDENTIALS) never appears anywhere in the checkpointed
# state / tool_results / final_output, even though every diagnostic tool
# resolves and uses it. This test runs a full invoke() with a distinctive
# mock credential value and asserts it never leaks into the result dict
# (which is exactly the shape LangGraph would checkpoint).

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.graph.graph import MonitoringAlertFirstTriageAgent

_DISTINCTIVE_CREDENTIAL = "mock-readonly-cred-DISTINCTIVE-MARKER-testing"

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


class ScriptedLLM:
    def __init__(self, script):
        self._script = script
        self._n = 0

    def complete(self, messages):
        response = self._script[self._n] if self._n < len(self._script) else {"content": "done", "tool_calls": []}
        self._n += 1
        return response

    def bind_tools(self, tools):
        return self

    def stream(self, messages):
        yield ""


def _contains_credential(obj) -> bool:
    """Recursively search a JSON-shaped structure for the distinctive marker."""
    text = json.dumps(obj, default=str)
    return _DISTINCTIVE_CREDENTIAL in text


class TestPB5CredentialNeverInCheckpointedOutput:
    def test_credential_not_in_final_invoke_result(self, tmp_path):
        runbook_file = tmp_path / "runbook.yaml"
        runbook_file.write_text(VALID_DSL)

        llm = ScriptedLLM(
            [
                {"content": "check", "tool_calls": [{"name": "http_get_healthcheck", "args": {"target": "portal-health:443"}}]},
                {"content": "done", "tool_calls": []},
            ]
        )
        agent = MonitoringAlertFirstTriageAgent(
            config={
                "llm": llm,
                "budget_usd": 1.50,
                "max_iterations": 24,
                "confidence_threshold": 0.75,
                "runbook_source": {"mode": "cached", "location": str(runbook_file)},
                "runbook_conversion": {"enabled": True, "cache_max_entries": 64},
                "target_allowlist": ["portal-health:443"],
                "reporting": {"enabled": True, "webhook_secret_key": "CHATOPS_WEBHOOK_URL", "min_interval_seconds": 0},
                "output": {"language": "ja"},
                "input_adapters": ["alertmanager", "datadog", "pagerduty", "generic"],
                "memory_enabled": False,
                "hitl": {"enabled": False},
            }
        )
        agent.compile()

        secrets = InMemoryProvider(
            {
                "DIAG_READONLY_CREDENTIALS": _DISTINCTIVE_CREDENTIAL,
                "CHATOPS_WEBHOOK_URL": "https://example.invalid/webhook",
            }
        )
        payload = json.dumps(
            {
                "groupKey": "x",
                "alerts": [{"labels": {"alertname": "A", "service": "web-portal", "severity": "critical"}}],
            }
        )

        with bound_secrets(secrets):
            ctx = InvocationContext(caller_trust_level=TrustLevel.INTERNAL)
            result = agent.invoke(payload, ctx=ctx)

        assert not _contains_credential(result), "read-only credential leaked into invoke() result (checkpoint-equivalent shape)"
