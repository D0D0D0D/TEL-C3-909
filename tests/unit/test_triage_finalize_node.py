# TEL-C3-909 — Unit Tests: TriageFinalizeNode

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.triage_finalize_node import TriageFinalizeNode, _is_assertive, _reframe_as_hypothesis


class FakeAgent:
    def __init__(self, assembled: dict):
        self._assembled = assembled

    def assemble_output(self, state):
        return self._assembled


def _base_state(status="pending"):
    return {
        "status": status,
        "node_history": [],
        "error_log": [],
        "execution_time": {},
        "correlation_id": "c",
        "session_id": "s",
        "trace_id": "t",
    }


class TestOnFinalizeCallsAssembleOutput:
    def test_calls_assemble_output_and_sets_final_output(self):
        agent = FakeAgent({"summary": {"status": "ok"}, "hypotheses": []})
        node = TriageFinalizeNode(agent)
        result = node.on_finalize(_base_state(status="pending"))
        assert result["final_output"] == agent._assembled

    def test_pending_status_becomes_success(self):
        agent = FakeAgent({"hypotheses": []})
        node = TriageFinalizeNode(agent)
        result = node.on_finalize(_base_state(status="pending"))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_prior_error_status_is_preserved_not_forced_to_success(self):
        agent = FakeAgent({"hypotheses": []})
        node = TriageFinalizeNode(agent)
        result = node.on_finalize(_base_state(status="error"))
        assert result["status"] == "error"

    def test_prior_cancelled_status_is_preserved(self):
        agent = FakeAgent({"hypotheses": []})
        node = TriageFinalizeNode(agent)
        result = node.on_finalize(_base_state(status="cancelled"))
        assert result["status"] == "cancelled"


class TestAssertivePhrasingDetection:
    def test_detects_japanese_assertive(self):
        assert _is_assertive("原因はDBコネクションプール枯渇です") is True

    def test_detects_english_assertive(self):
        assert _is_assertive("The cause is database exhaustion") is True

    def test_hypothesis_framing_not_flagged(self):
        assert _is_assertive("DBコネクションプール枯渇の可能性（確度0.8）") is False

    def test_fullwidth_variant_still_detected_via_nfkc(self):
        assert _is_assertive("原因は　ＤＢ枯渇です") is True

    def test_reframe_adds_hypothesis_marker(self):
        reframed = _reframe_as_hypothesis("原因はXです")
        assert "要確認" in reframed


class TestExtraSecurityGateOutput:
    def test_assertive_hypothesis_is_reframed(self):
        node = TriageFinalizeNode(FakeAgent({}))
        result = {
            "final_output": {
                "hypotheses": [{"cause": "原因はXです", "confidence": 0.9, "evidence": ["n1"]}],
                "escalation_required": False,
                "recommended_actions": [],
            }
        }
        out = node._extra_security_gate_output(result)
        assert "要確認" in out["final_output"]["hypotheses"][0]["cause"]

    def test_evidence_less_hypothesis_demoted(self):
        node = TriageFinalizeNode(FakeAgent({}))
        result = {
            "final_output": {
                "hypotheses": [{"cause": "disk full", "confidence": 0.9, "evidence": []}],
                "escalation_required": False,
                "recommended_actions": [],
            }
        }
        out = node._extra_security_gate_output(result)
        h = out["final_output"]["hypotheses"][0]
        assert h["confidence"] <= 0.3
        assert "要確認" in h["cause"]

    def test_credential_residue_in_report_draft_blocks_output(self):
        node = TriageFinalizeNode(FakeAgent({}))
        result = {
            "final_output": {
                "hypotheses": [],
                "escalation_required": False,
                "recommended_actions": [],
                "incident_report_draft": "leaked key sk-abcdefghijklmnopqrstuvwx",
            }
        }
        with pytest.raises(RuntimeError):
            node._extra_security_gate_output(result)

    def test_escalation_required_without_human_mention_blocks_output(self):
        node = TriageFinalizeNode(FakeAgent({}))
        result = {
            "final_output": {
                "hypotheses": [],
                "escalation_required": True,
                "recommended_actions": ["do something"],
            }
        }
        with pytest.raises(RuntimeError):
            node._extra_security_gate_output(result)

    def test_escalation_required_with_human_mention_passes(self):
        node = TriageFinalizeNode(FakeAgent({}))
        result = {
            "final_output": {
                "hypotheses": [],
                "escalation_required": True,
                "recommended_actions": ["エスカレーションが必要です"],
            }
        }
        out = node._extra_security_gate_output(result)
        assert out["final_output"]["escalation_required"] is True

    def test_non_dict_final_output_passthrough(self):
        node = TriageFinalizeNode(FakeAgent({}))
        result = {"final_output": None}
        assert node._extra_security_gate_output(result) == result


class TestTriageFinalizeNodeTrustLevel:
    def test_required_trust_level_is_verified_external(self):
        assert TriageFinalizeNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL


class TestRecursiveResidueScan:
    """review I-02: the S-3 (c) credential/PII residue scan must cover
    ALL string fields of final_output (recursive), not only incident_report_draft."""

    def _result_with(self, final_output: dict) -> dict:
        base = {
            "hypotheses": [],
            "escalation_required": False,
            "recommended_actions": [],
            "incident_report_draft": "clean draft",
        }
        base.update(final_output)
        return {"final_output": base}

    def test_credential_in_summary_top_hypothesis_blocks_output(self):
        node = TriageFinalizeNode(FakeAgent({}))
        result = self._result_with(
            {"summary": {"top_hypothesis": "cause includes sk-abcdefghijklmnopqrstuvwx", "next_action": "x"}}
        )
        with pytest.raises(RuntimeError, match="summary.top_hypothesis"):
            node._extra_security_gate_output(result)

    def test_credential_in_nested_report_log_blocks_output(self):
        node = TriageFinalizeNode(FakeAgent({}))
        result = self._result_with(
            {"report_log": [{"line": "pushed sk-abcdefghijklmnopqrstuvwx", "delivered": True}]}
        )
        with pytest.raises(RuntimeError, match=r"report_log\[0\]\.line"):
            node._extra_security_gate_output(result)

    def test_clean_nested_output_passes(self):
        node = TriageFinalizeNode(FakeAgent({}))
        result = self._result_with(
            {
                "summary": {"top_hypothesis": "DB コネクションプール枯渇（要確認・仮説）", "next_action": "L2 エスカレーション"},
                "report_log": [{"line": "[TEL-C3-909] web-portal: 手順3 → NG", "delivered": True}],
            }
        )
        out = node._extra_security_gate_output(result)
        assert out["final_output"]["summary"]["top_hypothesis"].startswith("DB")
