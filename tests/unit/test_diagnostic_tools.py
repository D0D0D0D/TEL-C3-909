# TEL-C3-909 — Unit Tests: diagnostic tools (get_tools() action space)

from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.services.readonly_gateway import ReadonlyGateway
from src.tools.diagnostic_tools import build_diagnostic_tools


def _tools_by_name(gateway):
    return {t.name: t for t in build_diagnostic_tools(gateway)}


class TestToolRegistry:
    def test_exactly_six_tools(self):
        gw = ReadonlyGateway(target_allowlist=[])
        tools = build_diagnostic_tools(gw)
        assert len(tools) == 6

    def test_expected_tool_names(self):
        gw = ReadonlyGateway(target_allowlist=[])
        names = {t.name for t in build_diagnostic_tools(gw)}
        assert names == {
            "http_get_healthcheck",
            "probe_connectivity",
            "read_metric",
            "tail_log",
            "check_liveness",
            "record_hypothesis",
        }


class TestRecordHypothesis:
    def test_no_external_reach_no_secrets_needed(self):
        gw = ReadonlyGateway(target_allowlist=[])
        tools = _tools_by_name(gw)
        # No bound_secrets context needed — must not touch ctx.secrets at all.
        result = tools["record_hypothesis"].invoke(
            {"cause": "possible DB exhaustion", "confidence": 0.7, "evidence": ["node-1"]}
        )
        assert result["cause"] == "possible DB exhaustion"
        assert result["confidence"] == 0.7
        assert result["evidence"] == ["node-1"]


class TestDiagnosticToolsRouteThroughGateway:
    def test_http_get_healthcheck_rejected_target(self):
        gw = ReadonlyGateway(target_allowlist=["allowed:80"])
        tools = _tools_by_name(gw)
        with bound_secrets(InMemoryProvider({"DIAG_READONLY_CREDENTIALS": "mock-readonly-cred-for-testing"})):
            result = tools["http_get_healthcheck"].invoke({"target": "not-allowed:80"})
        assert result["status"] == "rejected"

    def test_probe_connectivity_rejected_target(self):
        gw = ReadonlyGateway(target_allowlist=["allowed:80"])
        tools = _tools_by_name(gw)
        result = tools["probe_connectivity"].invoke({"target": "not-allowed:80"})
        assert result["status"] == "rejected"

    def test_read_metric_rejected_target(self):
        gw = ReadonlyGateway(target_allowlist=["allowed:80"])
        tools = _tools_by_name(gw)
        with bound_secrets(InMemoryProvider({"DIAG_READONLY_CREDENTIALS": "mock-readonly-cred-for-testing"})):
            result = tools["read_metric"].invoke({"target": "not-allowed:80", "metric": "cpu"})
        assert result["status"] == "rejected"

    def test_check_liveness_rejected_target(self):
        gw = ReadonlyGateway(target_allowlist=["allowed:80"])
        tools = _tools_by_name(gw)
        with bound_secrets(InMemoryProvider({"DIAG_READONLY_CREDENTIALS": "mock-readonly-cred-for-testing"})):
            result = tools["check_liveness"].invoke({"target": "not-allowed:80"})
        assert result["status"] == "rejected"

    def test_tail_log_rejected_target_and_scrubs_when_present(self):
        gw = ReadonlyGateway(target_allowlist=["allowed:80"])
        tools = _tools_by_name(gw)
        with bound_secrets(InMemoryProvider({"DIAG_READONLY_CREDENTIALS": "mock-readonly-cred-for-testing"})):
            result = tools["tail_log"].invoke({"target": "not-allowed:80", "lines": 10})
        assert result["status"] == "rejected"


class TestToolsAreMsgpackSafe:
    def test_all_tool_results_are_plain_dicts(self):
        gw = ReadonlyGateway(target_allowlist=[])
        tools = _tools_by_name(gw)
        r = tools["record_hypothesis"].invoke({"cause": "x", "confidence": 0.1, "evidence": []})
        assert isinstance(r, dict)
        for v in r.values():
            assert isinstance(v, (str, int, float, bool, list, dict, type(None)))
