"""AgentCore Platform v1.0"""

# TEL-C3-909 (docs/02_design.md §Node Configuration "assemble_output — 既知問題"):
# CASE 2 custom finalize node. The released wheel's FinalizeNode.on_finalize()
# default returns {} — assemble_output() is NEVER called by the framework on
# its own. This custom node calls it explicitly from on_finalize() (same
# pattern as Enterprise Deep Research & Long-Form Report Generation Agent) and preserves any prior terminal status (error /
# cancelled) rather than unconditionally forcing success — an unconditional
# SUCCESS override would hide a genuine S-1 rejection or budget cutoff
# behind a falsely-successful-looking report.
#
# S-1: VERIFIED_EXTERNAL — finalize must always run (including for the
# degraded VERIFIED_EXTERNAL-only invocation path where TriageActNode's
# S-1 gate rejected execution) so a partial/degraded report is still
# produced instead of the pipeline dying silently.

from __future__ import annotations

from typing import ClassVar

from framework.nodes.defaults.finalize_node import FinalizeNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event

# Assertive root-cause phrasing patterns to block/rewrite (docs/02 §S-3,
# item (a)). NFKC-normalize + casefold before matching so full/half-width
# and case variants can't bypass the check (禁止語/文言ガードは NFKC 正規化 +
# casefold 後に照合するのが実装契約).
import unicodedata

_ASSERTIVE_PATTERNS_JA = ("原因は", "が原因です", "が原因だ")
_ASSERTIVE_PATTERNS_EN = ("the cause is", "root cause is", "is the root cause")


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _is_assertive(text: str) -> bool:
    normalized = _normalize(text)
    return any(_normalize(p) in normalized for p in (*_ASSERTIVE_PATTERNS_JA, *_ASSERTIVE_PATTERNS_EN))


def _reframe_as_hypothesis(cause: str) -> str:
    return f"{cause}（要確認・仮説）" if not cause.endswith("（要確認・仮説）") else cause


def _iter_string_fields(obj, path: str = "final_output"):
    """Yield (path, value) for every string field reachable in a nested
    dict/list structure. Used by the S-3 (c) residue scan so that ALL
    string fields of final_output — including summary.top_hypothesis /
    summary.next_action, hypotheses[].cause, report_log lines, runbook_trace
    — are scanned, not only incident_report_draft (review I-02,
    defense-in-depth on top of the tool-boundary scrub)."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from _iter_string_fields(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            yield from _iter_string_fields(value, f"{path}[{i}]")


class TriageFinalizeNode(FinalizeNode):
    """Custom finalize node: calls assemble_output(), preserves prior status, applies S-3 domain gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, agent) -> None:
        self._agent = agent

    def on_finalize(self, state: AgentState) -> dict:
        prior_status = state.get("status", AgentStatus.PENDING.value)

        assembled = self._agent.assemble_output(state)

        emit_trace_event(
            "triage_assembled",
            {
                "prior_status": prior_status,
                "hypothesis_count": len(assembled.get("hypotheses", []) or []),
            },
            state,
        )

        # Preserve prior terminal status (error/cancelled) — do not force
        # success. Only upgrade PENDING (loop never reached a terminal
        # status naturally, e.g. degraded_mode short-circuit) to SUCCESS
        # since a partial/degraded report is still a completed triage pass.
        if prior_status in (AgentStatus.ERROR.value, AgentStatus.CANCELLED.value):
            final_status = prior_status
        else:
            final_status = AgentStatus.SUCCESS.value

        return {
            "final_output": assembled,
            "status": final_status,
        }

    # ------------------------------------------------------------------
    # S-3 domain extension — preservation + content-filtering variants
    # ------------------------------------------------------------------

    def _extra_security_gate_output(self, result: dict) -> dict:
        final_output = result.get("final_output")
        if not isinstance(final_output, dict):
            return result

        result = dict(result)
        final_output = dict(final_output)

        # (a) Block assertive root-cause phrasing -> force hypothesis framing.
        hypotheses = final_output.get("hypotheses", []) or []
        rewritten_hypotheses = []
        for h in hypotheses:
            if not isinstance(h, dict):
                rewritten_hypotheses.append(h)
                continue
            h = dict(h)
            cause = str(h.get("cause", ""))
            if _is_assertive(cause):
                h["cause"] = _reframe_as_hypothesis(cause)
            # (b) evidence-less hypotheses are demoted to "要確認".
            if not h.get("evidence"):
                h["confidence"] = min(float(h.get("confidence", 0.0)), 0.3)
                h["cause"] = f"{h['cause']}（根拠不足のため要確認）" if "要確認" not in h["cause"] else h["cause"]
            rewritten_hypotheses.append(h)
        final_output["hypotheses"] = rewritten_hypotheses

        # (c) Final credential/PII residue check across ALL string fields of
        # final_output (recursive walk — summary.top_hypothesis / next_action,
        # hypotheses[].cause, report_log, runbook_trace included), not only
        # incident_report_draft (review I-02).
        for field_path, value in _iter_string_fields(final_output):
            findings = list(detect_credentials(value)) + list(detect_pii(value))
            if findings:
                raise RuntimeError(
                    f"[TriageFinalizeNode] S-3 domain gate: credential/PII residue detected in "
                    f"{field_path} — output blocked"
                )

        # (d) Preservation variant: escalation_required=true implies
        # recommended_actions mentions human/escalation requirement. Reject
        # (raise) rather than silently drop this — the whole point is that
        # dropping the escalation notice must be caught, not filtered away.
        if final_output.get("escalation_required"):
            actions_text = " ".join(str(a) for a in final_output.get("recommended_actions", []) or [])
            if "エスカレーション" not in actions_text and "escalat" not in actions_text.lower() and "人間" not in actions_text:
                raise RuntimeError(
                    "[TriageFinalizeNode] S-3 preservation check: escalation_required=True but "
                    "recommended_actions does not mention human escalation — output blocked"
                )

        result["final_output"] = final_output
        return result
