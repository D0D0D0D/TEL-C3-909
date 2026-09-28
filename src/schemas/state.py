"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.
#
# TEL-C3-909 (docs/02_design.md §State Definition): this agent is Cat 3
# (AutonomousBaseGraph) and uses the framework's `AutonomousState` AS-IS.
# It is re-exported here (not redeclared/subclassed) because the design
# deliberately keeps all domain data inside the existing `working_memory`
# (dict) and `tool_results` (list[dict]) fields rather than adding new
# top-level State keys. No agent-specific fields are declared.
#
# `working_memory` key contract (msgpack-safe only — primitives/dict/list):
#
#   alerts (list[dict]):
#       Normalized alerts for the whole correlation group, one entry per
#       alert: {alert_id, target_system, alert_type, severity, ts, metrics}.
#
#   runbook_ref (dict):
#       Metadata about the runbook that matched this alert group:
#       {runbook_id, version, source_mode, doc_hash, fetched_at,
#        converted(bool), cache_hit(bool), redacted_credential_count}.
#       This is the "transparency" record surfaced in the final report's
#       `runbook_trace` section — it lets a human verify which version of
#       the runbook the agent read and how it was interpreted.
#
#   decision_tree (dict):
#       The canonical DSL decision tree the agent is allowed to traverse
#       (nodes = read-only checks, guarded by `on_ok` / `on_ng` edges,
#       referencing only registered read-only tool names). This is the
#       agent's entire action space for this invocation.
#
#   visited (list[dict]):
#       Checks already performed, shared across all correlated alerts in
#       the same payload: {node_id, target, tool, result, verdict, alert_ids}.
#       `verdict` is one of "ok" / "ng" / "failed".
#
#   hypotheses (list[dict]):
#       Root-cause hypotheses accumulated via the `record_hypothesis` tool:
#       {cause, confidence, evidence}. `evidence` is a list of `node_id`
#       references into `visited` — never raw log/connection data (S-5).
#       Never phrased as an assertive claim ("the cause is X") — always a
#       hypothesis with a confidence score and traceable evidence.
#
#   escalation_required (bool):
#       True when a diagnostic step outside the read-only allowlist would be
#       needed to continue — i.e. human intervention is required.
#
#   report_log (list[dict]):
#       Incremental ChatOps reports already pushed (or attempted). Failed
#       pushes are recorded as {"delivered": False, ...} rather than dropped
#       silently, so the final report can surface undelivered updates.
#
#   degraded_mode (bool):
#       True when the runbook could not be fetched, no decision tree could
#       be matched, OR the caller's trust level is insufficient for full
#       diagnostic execution (see degraded_reason). In this mode initialize
#       completes without running any diagnostics — the agent still reports
#       "received / identified target / <reason>" rather than doing nothing.
#
#   degraded_reason (str):
#       Machine-readable reason code for degraded_mode: "" (not degraded),
#       "insufficient_trust_for_diagnostics", "runbook_location_not_configured",
#       "runbook_fetch_failed", "runbook_validation_failed", or
#       "no_decision_tree_matched". Set by TriageInitializeNode and consumed
#       by build_think_prompt() (to give the LLM a hard "never emit
#       tool_calls" instruction) and assemble_output() (to phrase the report
#       precisely). See implementation note below on why
#       "insufficient_trust_for_diagnostics" is detected pre-emptively at
#       initialize time rather than left to TriageActNode's own S-1 gate.
#
# Implementation note (found during implementation, 2026-07-06): the
# released wheel's BaseNode.__call__()
# unconditionally skips execute() (and therefore on_finalize() /
# assemble_output()) for ANY FunctionNode when the incoming
# state["status"] is already the literal string "error" — this is
# independent of that node's own S-2 hook logic. Verified directly against
# framework/nodes/function_node.py + base_node.py (not a design assumption).
# Consequence: if TriageActNode's own S-1 gate denies (VERIFIED_EXTERNAL
# caller), the resulting status="error" propagates through the fixed
# act->think->finalize edges and TriageFinalizeNode.on_finalize() would
# NEVER run — silently defeating the "finalize always produces a partial
# report" design guarantee for that specific path. Mitigation implemented
# in TriageInitializeNode.on_initialize(): detect insufficient trust level
# BEFORE the loop starts and force degraded_mode=True (with
# degraded_reason="insufficient_trust_for_diagnostics"), so a
# prompt-compliant LLM never emits tool_calls and `act` is never reached.
# This is NOT a fully airtight guarantee (an LLM could still hallucinate a
# tool_call despite the hard instruction in build_think_prompt()) — if that
# happens, TriageActNode's S-1 gate still denies it, but the resulting
# status="error" will cause finalize to be skipped and output=None, same as
# any other unrecovered "error" status reaching finalize. This residual gap
# is a framework-level limitation, not fixable from template code without
# overriding __call__ (prohibited) or overriding `think` (prohibited by
# docs/02's CASE 2 contract for this template). See docs/02 Open Items /
# work/03_review_guide.md for the full note to reviewers.

# State Constraints (docs/02_design.md, mandatory):
#   - Flat TypedDict only (primitives + JSON-serializable containers).
#   - No credentials of any kind in State — diagnostic target connection
#     info is resolved by the tool/service layer via ctx.secrets.require()
#     on every call and never written back into State/working_memory.
#   - Runbook text is only ever placed into State/prompts AFTER credential
#     scrubbing (see src/services/runbook_service.py). The scrub is a
#     defense-in-depth measure only — actual connection secrets always come
#     from ctx.secrets, never from runbook text, even if scrubbing is
#     imperfect.
#   - Tool outputs (e.g. tail_log) are scrubbed of credentials/PII in the
#     service layer *before* they are returned to the node — tool_results is
#     checkpointed, so contamination there is a checkpoint-safety violation.
#   - InvocationContext is never placed in State — accessed only via
#     config["configurable"] / InvocationContext.from_state(state).

from framework.schemas.autonomous_state import AutonomousState

# Re-exported, not subclassed: this agent adds no new top-level State keys.
State = AutonomousState
