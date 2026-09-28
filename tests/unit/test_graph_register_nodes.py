# TEL-C3-909 — Unit Tests: register_nodes() CASE 2 composition
# (initialize/act/finalize replaced; think left as framework default)

from framework.nodes.defaults.think_node import ThinkNode

from src.graph.graph import MonitoringAlertFirstTriageAgent
from src.nodes.triage_act_node import TriageActNode
from src.nodes.triage_finalize_node import TriageFinalizeNode
from src.nodes.triage_initialize_node import TriageInitializeNode


class FakeLLM:
    def complete(self, messages):
        return {"content": "", "tool_calls": []}

    def bind_tools(self, tools):
        return self

    def stream(self, messages):
        yield ""


def _config():
    return {
        "llm": FakeLLM(),
        "budget_usd": 1.50,
        "max_iterations": 24,
        "confidence_threshold": 0.75,
        "runbook_source": {"mode": "cached", "location": ""},
        "runbook_conversion": {"enabled": True, "cache_max_entries": 64},
        "target_allowlist": [],
        "reporting": {"enabled": True, "webhook_secret_key": "CHATOPS_WEBHOOK_URL", "min_interval_seconds": 0},
        "output": {"language": "ja"},
        "input_adapters": ["alertmanager", "datadog", "pagerduty", "generic"],
        "memory_enabled": False,
        "hitl": {"enabled": False},
    }


class TestRegisterNodesCase2:
    def test_all_four_slots_present_after_register(self):
        agent = MonitoringAlertFirstTriageAgent(config=_config())
        agent.compile()
        assert set(agent._nodes.keys()) == {"initialize", "think", "act", "finalize"}

    def test_initialize_is_custom_triage_node(self):
        agent = MonitoringAlertFirstTriageAgent(config=_config())
        agent.compile()
        assert isinstance(agent._nodes["initialize"], TriageInitializeNode)

    def test_act_is_custom_triage_node(self):
        agent = MonitoringAlertFirstTriageAgent(config=_config())
        agent.compile()
        assert isinstance(agent._nodes["act"], TriageActNode)

    def test_finalize_is_custom_triage_node(self):
        agent = MonitoringAlertFirstTriageAgent(config=_config())
        agent.compile()
        assert isinstance(agent._nodes["finalize"], TriageFinalizeNode)

    def test_think_is_framework_default_not_replaced(self):
        agent = MonitoringAlertFirstTriageAgent(config=_config())
        agent.compile()
        assert type(agent._nodes["think"]) is ThinkNode

    def test_add_edges_not_overridden(self):
        """Cat 3 contract: add_edges()/_route_after_think() must not be
        overridden by the template — verify no override exists on the class
        itself (inherited from AutonomousBaseGraph only)."""
        assert "add_edges" not in MonitoringAlertFirstTriageAgent.__dict__
        assert "_route_after_think" not in MonitoringAlertFirstTriageAgent.__dict__
        assert "route" not in MonitoringAlertFirstTriageAgent.__dict__


class TestGraphAlias:
    def test_graph_alias_points_to_agent_class(self):
        from src.graph.graph import Graph

        assert Graph is MonitoringAlertFirstTriageAgent
