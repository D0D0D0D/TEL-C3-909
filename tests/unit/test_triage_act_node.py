# TEL-C3-909 — Unit Tests: TriageActNode

from unittest.mock import patch

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.triage_act_node import TriageActNode


class FakeTool:
    def __init__(self, name, result=None, raises=None):
        self.name = name
        self._result = result or {"status": "ok"}
        self._raises = raises

    def invoke(self, args):
        if self._raises:
            raise self._raises
        return self._result


def _base_state(tool_calls, working_memory=None):
    return {
        "tool_calls": tool_calls,
        "working_memory": working_memory or {"alerts": [], "visited": [], "report_log": [], "hypotheses": [], "decision_tree": {}},
        "caller_trust_level": TrustLevel.INTERNAL.value,
        "correlation_id": "c",
        "session_id": "s",
        "trace_id": "t",
        "node_history": [],
        "error_log": [],
    }


class TestTriageActNodeExecute:
    def test_no_tool_calls_returns_empty(self):
        node = TriageActNode([FakeTool("http_get_healthcheck")], reporting_enabled=False)
        result = node.execute(_base_state([]))
        assert result == {"tool_calls": [], "tool_results": []}

    def test_unknown_tool_returns_error(self):
        node = TriageActNode([FakeTool("http_get_healthcheck")], reporting_enabled=False)
        result = node.execute(_base_state([{"name": "unknown_tool", "args": {}}]))
        assert result["status"] == AgentStatus.ERROR.value

    def test_tool_exception_returns_error_and_preserves_prior_results(self):
        node = TriageActNode([FakeTool("bad_tool", raises=RuntimeError("boom"))], reporting_enabled=False)
        result = node.execute(_base_state([{"name": "bad_tool", "args": {}}]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "boom" in result["error_log"][0]

    def test_record_hypothesis_does_not_add_visited_entry(self):
        node = TriageActNode(
            [FakeTool("record_hypothesis", result={"cause": "x", "confidence": 0.5, "evidence": []})],
            reporting_enabled=False,
        )
        result = node.execute(_base_state([{"name": "record_hypothesis", "args": {"cause": "x", "confidence": 0.5, "evidence": []}}]))
        assert result["working_memory"]["hypotheses"] == [{"cause": "x", "confidence": 0.5, "evidence": []}]
        assert result["working_memory"]["visited"] == []

    def test_diagnostic_tool_adds_visited_entry(self):
        node = TriageActNode(
            [FakeTool("http_get_healthcheck", result={"target": "t:1", "ok": True, "status": "ok", "detail": {}})],
            reporting_enabled=False,
        )
        result = node.execute(_base_state([{"name": "http_get_healthcheck", "args": {"target": "t:1"}}]))
        assert len(result["working_memory"]["visited"]) == 1
        assert result["working_memory"]["visited"][0]["verdict"] == "ok"

    def test_reporting_disabled_skips_push(self):
        node = TriageActNode(
            [FakeTool("http_get_healthcheck", result={"target": "t:1", "ok": True, "status": "ok", "detail": {}})],
            reporting_enabled=False,
        )
        result = node.execute(_base_state([{"name": "http_get_healthcheck", "args": {"target": "t:1"}}]))
        assert result["working_memory"]["report_log"] == []

    def test_reporting_enabled_pushes_and_records_delivery(self):
        node = TriageActNode(
            [FakeTool("http_get_healthcheck", result={"target": "t:1", "ok": True, "status": "ok", "detail": {}})],
            reporting_enabled=True,
        )
        secrets = InMemoryProvider({"CHATOPS_WEBHOOK_URL": "https://example.invalid/webhook"})
        with bound_secrets(secrets), patch("src.nodes.triage_act_node.httpx.post") as mock_post:
            mock_post.return_value = None
            result = node.execute(_base_state([{"name": "http_get_healthcheck", "args": {"target": "t:1"}}]))
        assert len(result["working_memory"]["report_log"]) == 1
        assert result["working_memory"]["report_log"][0]["delivered"] is True

    def test_reporting_failure_retries_then_records_undelivered_and_continues(self):
        import httpx as httpx_module

        node = TriageActNode(
            [FakeTool("http_get_healthcheck", result={"target": "t:1", "ok": True, "status": "ok", "detail": {}})],
            reporting_enabled=True,
        )
        secrets = InMemoryProvider({"CHATOPS_WEBHOOK_URL": "https://example.invalid/webhook"})
        with bound_secrets(secrets), patch(
            "src.nodes.triage_act_node.httpx.post", side_effect=httpx_module.ConnectError("down")
        ) as mock_post:
            result = node.execute(_base_state([{"name": "http_get_healthcheck", "args": {"target": "t:1"}}]))
        assert mock_post.call_count == 2  # 1 try + 1 retry
        assert result["working_memory"]["report_log"][0]["delivered"] is False
        # loop must not be blocked — tool_results still populated
        assert len(result["tool_results"]) == 1

    def test_missing_webhook_secret_does_not_crash(self):
        node = TriageActNode(
            [FakeTool("http_get_healthcheck", result={"target": "t:1", "ok": True, "status": "ok", "detail": {}})],
            reporting_enabled=True,
        )
        with bound_secrets(InMemoryProvider({})):
            result = node.execute(_base_state([{"name": "http_get_healthcheck", "args": {"target": "t:1"}}]))
        assert result["working_memory"]["report_log"][0]["delivered"] is False


class TestTriageActNodeTrustGate:
    def test_internal_only_class_attribute(self):
        assert TriageActNode.required_trust_level == TrustLevel.INTERNAL

    def test_call_denies_verified_external_caller(self):
        node = TriageActNode([FakeTool("http_get_healthcheck")], reporting_enabled=False)
        state = _base_state([{"name": "http_get_healthcheck", "args": {"target": "t:1"}}])
        state["caller_trust_level"] = TrustLevel.VERIFIED_EXTERNAL.value
        result = node(state)  # via __call__, not execute() — exercises S-1 gate
        assert result["status"] == AgentStatus.ERROR.value
        assert "S-1 trust gate denied" in result["error_log"][0]

    def test_call_allows_internal_caller(self):
        node = TriageActNode(
            [FakeTool("http_get_healthcheck", result={"target": "t:1", "ok": True, "status": "ok", "detail": {}})],
            reporting_enabled=False,
        )
        state = _base_state([{"name": "http_get_healthcheck", "args": {"target": "t:1"}}])
        with bound_secrets(InMemoryProvider({})):
            result = node(state)
        # execute() does not set "status" on the success path (only on
        # error paths) — absence of "status"/error_log is itself the
        # positive signal that the S-1 gate allowed execution through.
        assert result.get("status") != AgentStatus.ERROR.value
        assert "tool_results" in result
        assert len(result["tool_results"]) == 1
