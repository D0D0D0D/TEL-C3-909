# TEL-C3-909 — Unit Tests: MonitoringAlertFirstTriageAgent abstract surface
# (max_iterations, get_tools, build_think_prompt, assemble_output)

from src.graph.graph import MonitoringAlertFirstTriageAgent


class FakeLLM:
    def complete(self, messages):
        return {"content": "", "tool_calls": []}

    def bind_tools(self, tools):
        return self

    def stream(self, messages):
        yield ""


def _agent(**config_overrides):
    config = {
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
    config.update(config_overrides)
    return MonitoringAlertFirstTriageAgent(config=config)


class TestMaxIterations:
    def test_reads_from_config(self):
        agent = _agent(max_iterations=10)
        assert agent.max_iterations == 10

    def test_defaults_when_absent(self):
        config = {"llm": FakeLLM(), "budget_usd": 1.0}
        agent = MonitoringAlertFirstTriageAgent(config=config)
        assert agent.max_iterations == MonitoringAlertFirstTriageAgent.DEFAULT_MAX_ITERATIONS == 24


class TestGetTools:
    def test_returns_six_tools(self):
        agent = _agent()
        tools = agent.get_tools()
        assert len(tools) == 6

    def test_tool_names_match_dsl_action_space(self):
        agent = _agent()
        names = {t.name for t in agent.get_tools()}
        assert names == {
            "http_get_healthcheck",
            "probe_connectivity",
            "read_metric",
            "tail_log",
            "check_liveness",
            "record_hypothesis",
        }


class TestBuildThinkPrompt:
    def test_degraded_mode_forbids_tool_calls_in_prompt_text(self):
        agent = _agent()
        state = {
            "working_memory": {"degraded_mode": True, "degraded_reason": "runbook_fetch_failed", "alerts": []},
            "iterations": 0,
            "cost_usd": 0.0,
        }
        prompt = agent.build_think_prompt(state)
        assert "tool" in prompt  # mentions the restriction
        assert "runbook_fetch_failed" in prompt

    def test_insufficient_trust_reason_surfaced_distinctly(self):
        agent = _agent()
        state = {
            "working_memory": {"degraded_mode": True, "degraded_reason": "insufficient_trust_for_diagnostics", "alerts": []},
            "iterations": 0,
            "cost_usd": 0.0,
        }
        prompt = agent.build_think_prompt(state)
        assert "INTERNAL" in prompt

    def test_normal_mode_includes_decision_tree_and_thresholds(self):
        agent = _agent()
        state = {
            "working_memory": {
                "degraded_mode": False,
                "decision_tree": {"nodes": [{"id": "a"}]},
                "visited": [],
                "hypotheses": [],
                "alerts": [],
            },
            "iterations": 1,
            "cost_usd": 0.05,
        }
        prompt = agent.build_think_prompt(state)
        assert "confidence_threshold" in prompt or "確度しきい値" in prompt
        assert "record_hypothesis" in prompt


class TestAssembleOutput:
    def test_degraded_mode_output_has_all_required_sections(self):
        agent = _agent()
        state = {
            "working_memory": {
                "alerts": [{"alert_id": "a1"}],
                "visited": [],
                "decision_tree": {},
                "hypotheses": [],
                "runbook_ref": {},
                "report_log": [],
                "degraded_mode": True,
                "degraded_reason": "runbook_fetch_failed",
                "escalation_required": False,
            },
            "status": "pending",
        }
        output = agent.assemble_output(state)
        for key in (
            "summary",
            "checked_matrix",
            "unchecked",
            "hypotheses",
            "recommended_actions",
            "incident_report_draft",
            "report_log",
            "runbook_trace",
        ):
            assert key in output
        assert output["escalation_required"] is True

    def test_cancelled_status_preserved_in_summary(self):
        agent = _agent()
        state = {
            "working_memory": {
                "alerts": [],
                "visited": [{"node_id": "a:x:0"}],
                "decision_tree": {"nodes": [{"id": "a"}, {"id": "b"}]},
                "hypotheses": [],
                "runbook_ref": {},
                "report_log": [],
                "degraded_mode": False,
                "escalation_required": False,
            },
            "status": "cancelled",
        }
        output = agent.assemble_output(state)
        assert "打ち切り" in output["summary"]["status"]
        assert output["escalation_required"] is True

    def test_success_with_high_confidence_hypothesis(self):
        agent = _agent(confidence_threshold=0.75)
        state = {
            "working_memory": {
                "alerts": [],
                "visited": [{"node_id": "a:x:0"}],
                "decision_tree": {"nodes": [{"id": "a"}]},
                "hypotheses": [{"cause": "db exhaustion", "confidence": 0.9, "evidence": ["a:x:0"]}],
                "runbook_ref": {},
                "report_log": [],
                "degraded_mode": False,
                "escalation_required": False,
            },
            "status": "success",
        }
        output = agent.assemble_output(state)
        assert "0.90" in output["summary"]["top_hypothesis"]
        assert output["escalation_required"] is False

    def test_never_returns_none_on_error_status(self):
        """Guards the 'always produce a partial report' promise at the
        assemble_output level (independent of whether finalize's execute()
        is reached — see state.py implementation note for the residual
        framework-level gap when status literally becomes 'error' before
        finalize)."""
        agent = _agent()
        state = {
            "working_memory": {
                "alerts": [],
                "visited": [],
                "decision_tree": {},
                "hypotheses": [],
                "runbook_ref": {},
                "report_log": [],
                "degraded_mode": True,
                "degraded_reason": "insufficient_trust_for_diagnostics",
                "escalation_required": True,
            },
            "status": "error",
        }
        output = agent.assemble_output(state)
        assert output is not None
        assert "INTERNAL" in output["summary"]["status"] or "実行不可" in output["summary"]["status"]
