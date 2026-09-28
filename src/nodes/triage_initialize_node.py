"""AgentCore Platform v1.0"""

# TEL-C3-909 (docs/02_design.md §Node Configuration / §Data Flow): CASE 2
# custom initialize node. Extends the framework's AutonomousInitializeNode
# via the on_initialize() extension hook (not by re-implementing _setup()),
# so the framework's loop-reset fields (iterations=0, tool_calls=[], etc.)
# are always applied first.
#
# Responsibilities (docs/02 Node Configuration table, "initialize" row):
#   - Normalize the raw webhook payload (user_input) into canonical alerts.
#   - Fetch the current runbook, credential-scrub it, convert to DSL at
#     runtime if needed, validate, and match a decision tree to the alert.
#   - On any failure along that path: enter degraded_mode (no diagnostics,
#     but still "received / target identified" reporting) rather than
#     silently doing nothing.
#   - Emit S-4 domain events: alert_received / runbook_fetched /
#     runbook_converted / runbook_matched (as applicable).
#
# S-1: VERIFIED_EXTERNAL (docs/02 §Security Model S-1) — this node runs for
# both the standard INTERNAL webhook-receiver caller and any VERIFIED_EXTERNAL
# caller; it is TriageActNode (INTERNAL-only) that gates full diagnostic
# execution.

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.defaults.autonomous_initialize_node import AutonomousInitializeNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.alert_adapters import normalize_alerts
from src.services.readonly_gateway import ReadonlyGateway
from src.services.runbook_service import (
    RunbookFetchError,
    RunbookValidationError,
    fetch_runbook_text,
    load_decision_tree,
    match_tree_to_alert,
    now_iso,
)

# Instruction-like text heuristic reused for alert free-text fields
# (docs/02 §S-2, item (b) prompt-injection separation). A dedicated,
# lighter check than the DSL-node one — alert annotations/labels are
# untrusted external text and must never be treated as instructions.
_INSTRUCTION_MARKERS = ("ignore previous", "無視して", "system prompt", "you are now")


def _looks_instruction_like(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _INSTRUCTION_MARKERS)


class TriageInitializeNode(AutonomousInitializeNode):
    """Custom initialize node: alert normalization + runbook fetch/convert/match."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, agent) -> None:
        # `agent` is the owning AutonomousBaseGraph instance — used to reach
        # config (runbook_source, target_allowlist, runbook_conversion) and
        # the constructor-injected LLM (docs/02: runtime conversion uses the
        # same BaseLLM instance by default; `runbook_conversion.llm` optional).
        self._agent = agent

    # ------------------------------------------------------------------
    # S-2 domain extension
    # ------------------------------------------------------------------

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        """Domain S-2: alert JSON schema/size validation + prompt-injection
        separation + unknown runbook_id rejection.

        Credential scrubbing of the *runbook text* itself is performed in
        on_initialize() / runbook_service (single location, documented
        choice) — this hook only validates the raw alert payload shape,
        which is the untrusted external input available before execute().
        """
        state = dict(state)
        user_input = state.get("user_input", "")
        if not isinstance(user_input, str) or not user_input.strip():
            state["status"] = AgentStatus.ERROR.value
            state["error_log"] = ["[TriageInitializeNode] empty alert payload"]
            return state

        # Size cap — a pathologically large payload is rejected rather than
        # parsed (defends against resource-exhaustion / injection-by-volume).
        if len(user_input) > 200_000:
            state["status"] = AgentStatus.ERROR.value
            state["error_log"] = ["[TriageInitializeNode] alert payload exceeds size limit"]
            return state

        try:
            payload = json.loads(user_input)
        except (json.JSONDecodeError, TypeError):
            state["status"] = AgentStatus.ERROR.value
            state["error_log"] = ["[TriageInitializeNode] alert payload is not valid JSON"]
            return state

        if not isinstance(payload, dict):
            state["status"] = AgentStatus.ERROR.value
            state["error_log"] = ["[TriageInitializeNode] alert payload must be a JSON object"]
            return state

        # Prompt-injection separation: scan free-text fields commonly used
        # for annotations/labels for instruction-like content. We do not
        # reject on this alone (it is observed data, not a command) but we
        # flag it into input_context so build_think_prompt() can mark it as
        # "observed data" rather than treating it as an instruction.
        flat_text = json.dumps(payload)[:5000]
        if _looks_instruction_like(flat_text):
            input_context = dict(state.get("input_context", {}))
            input_context["alert_contains_instruction_like_text"] = True
            state["input_context"] = input_context

        return state

    # ------------------------------------------------------------------
    # Custom initialization
    # ------------------------------------------------------------------

    def on_initialize(self, state: AgentState) -> dict:
        config = getattr(self._agent, "config", {}) or {}
        emit_trace_event("alert_received", {"raw_len": len(state.get("user_input", ""))}, state)

        try:
            payload = json.loads(state.get("user_input", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        enabled_adapters = config.get("input_adapters") or ["alertmanager", "datadog", "pagerduty", "generic"]
        alerts, adapter_used = normalize_alerts(payload, enabled_adapters)

        working_memory: dict = {
            "alerts": alerts,
            "runbook_ref": {},
            "decision_tree": {},
            "visited": [],
            "hypotheses": [],
            "escalation_required": False,
            "report_log": [],
            "degraded_mode": False,
            "degraded_reason": "",
        }

        # S-1 caller contract pre-emption (docs/02_design.md §Security Model
        # S-1 "呼び出し元契約"): full diagnostic execution only happens via
        # TriageActNode, which requires INTERNAL trust. If the caller is not
        # INTERNAL, do not let the loop reach `act` at all — the framework's
        # own S-1 gate on TriageActNode would deny it there, but that denial
        # sets state["status"]="error" *before* finalize's turn, and every
        # FunctionNode.__call__ (including TriageFinalizeNode) unconditionally
        # skips execute() when incoming status is already "error" (verified
        # framework behavior — not a hypothesis). That would silently defeat
        # the "always produce a partial report" guarantee. Detecting the
        # insufficient trust level here — before the loop starts — and
        # forcing degraded_mode=True (no decision_tree => build_think_prompt
        # instructs the LLM to never emit tool_calls) keeps `act` from ever
        # being reached, so finalize runs normally and reports the
        # trust-level limitation explicitly instead of erroring out.
        caller_trust = state.get("caller_trust_level", TrustLevel.ANONYMOUS.value)
        if caller_trust != TrustLevel.INTERNAL.value:
            working_memory["degraded_mode"] = True
            working_memory["degraded_reason"] = "insufficient_trust_for_diagnostics"
            working_memory["escalation_required"] = True
            emit_trace_event(
                "runbook_matched",
                {"result": "insufficient_trust_for_diagnostics", "caller_trust_level": caller_trust},
                state,
            )
            return {
                "working_memory": working_memory,
                "status": AgentStatus.PENDING.value,
            }

        runbook_source = config.get("runbook_source", {}) or {}
        location = runbook_source.get("location", "")
        cache_mode = runbook_source.get("mode", "cached")
        runbook_conversion = config.get("runbook_conversion", {}) or {}
        conversion_enabled = runbook_conversion.get("enabled", True)
        cache_max_entries = runbook_conversion.get("cache_max_entries", 64)

        target_allowlist = config.get("target_allowlist") or []
        gateway = ReadonlyGateway(target_allowlist=target_allowlist)

        registered_tool_names = {t.name for t in self._agent.get_tools()}

        if not location:
            working_memory["degraded_mode"] = True
            working_memory["degraded_reason"] = "runbook_location_not_configured"
            emit_trace_event(
                "runbook_fetched",
                {"source": location, "result": "empty_location", "adapter_used": adapter_used},
                state,
            )
            return {
                "working_memory": working_memory,
                "status": AgentStatus.PENDING.value,
            }

        try:
            raw_text = fetch_runbook_text(location, gateway=gateway)
        except RunbookFetchError as exc:
            working_memory["degraded_mode"] = True
            working_memory["degraded_reason"] = "runbook_fetch_failed"
            emit_trace_event("runbook_fetched", {"source": location, "result": "failed", "error": str(exc)}, state)
            return {
                "working_memory": working_memory,
                "status": AgentStatus.PENDING.value,
            }

        emit_trace_event("runbook_fetched", {"source": location, "result": "ok", "adapter_used": adapter_used}, state)

        llm = config.get("llm")
        try:
            load_result = load_decision_tree(
                raw_text=raw_text,
                registered_tool_names=registered_tool_names,
                llm=llm,
                cache_enabled=(cache_mode == "cached") and conversion_enabled,
                cache_max_entries=cache_max_entries,
                conversion_enabled=conversion_enabled,
            )
        except RunbookValidationError as exc:
            working_memory["degraded_mode"] = True
            working_memory["degraded_reason"] = "runbook_validation_failed"
            emit_trace_event(
                "runbook_converted",
                {"result": "validation_failed", "error": str(exc)},
                state,
            )
            return {
                "working_memory": working_memory,
                "status": AgentStatus.PENDING.value,
            }

        emit_trace_event(
            "runbook_converted",
            {
                "node_count": len(load_result["decision_tree"].get("nodes", [])),
                "converted": load_result["converted"],
                "cache_hit": load_result["cache_hit"],
                "redacted_credential_count": load_result["redacted_credential_count"],
            },
            state,
        )

        tree = load_result["decision_tree"]
        matched = any(match_tree_to_alert(tree, a) for a in alerts) if alerts else False

        working_memory["runbook_ref"] = {
            "runbook_id": tree.get("runbook_id", ""),
            "version": tree.get("version", ""),
            "source_mode": cache_mode,
            "doc_hash": load_result["doc_hash"],
            "fetched_at": now_iso(),
            "converted": load_result["converted"],
            "cache_hit": load_result["cache_hit"],
            "redacted_credential_count": load_result["redacted_credential_count"],
        }

        if matched:
            working_memory["decision_tree"] = tree
            emit_trace_event(
                "runbook_matched",
                {"runbook_id": tree.get("runbook_id", ""), "doc_hash": load_result["doc_hash"]},
                state,
            )
        else:
            working_memory["degraded_mode"] = True
            working_memory["degraded_reason"] = "no_decision_tree_matched"
            emit_trace_event(
                "runbook_matched",
                {"result": "no_match", "doc_hash": load_result["doc_hash"]},
                state,
            )

        return {
            "working_memory": working_memory,
            "status": AgentStatus.PENDING.value,
        }
