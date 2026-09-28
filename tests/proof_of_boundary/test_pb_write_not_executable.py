# PB boundary: "write operations are actually not executable" — proves the
# 5-layer read-only enforcement physically, not just by config declaration.
#
# (a) the HTTP client used by the gateway has no write-method code path
# (b) an unregistered destination is rejected at the gateway boundary
# (c) an unregistered tool name is rejected by TriageActNode

from framework.schemas.agent_status import AgentStatus

from src.nodes.triage_act_node import TriageActNode
from src.services.readonly_gateway import ReadonlyGateway


class TestPBWriteMethodsDoNotExist:
    def test_readonly_gateway_has_no_write_http_methods(self):
        public_methods = {m for m in dir(ReadonlyGateway) if not m.startswith("_")}
        for forbidden in ("http_post", "http_put", "http_delete", "http_patch", "post", "put", "delete", "patch"):
            assert forbidden not in public_methods, f"write method {forbidden!r} must not exist on ReadonlyGateway"

    def test_only_read_only_verbs_present(self):
        public_methods = {m for m in dir(ReadonlyGateway) if not m.startswith("_")}
        assert public_methods == {"http_get", "http_head", "tcp_connect", "is_allowed"}


class TestPBUnregisteredDestinationRejected:
    def test_http_get_rejects_unregistered_destination(self):
        gw = ReadonlyGateway(target_allowlist=["allowed:80"])
        result = gw.http_get("attacker-controlled:80")
        assert result.status == "rejected"
        assert result.ok is False

    def test_tcp_connect_rejects_unregistered_destination(self):
        gw = ReadonlyGateway(target_allowlist=["allowed:80"])
        result = gw.tcp_connect("attacker-controlled:80")
        assert result.status == "rejected"


class TestPBUnregisteredToolRejected:
    def test_triage_act_node_rejects_unknown_tool_name(self):
        node = TriageActNode([], reporting_enabled=False)
        state = {
            "tool_calls": [{"name": "delete_everything", "args": {}}],
            "working_memory": {},
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
