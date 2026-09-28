"""AgentCore Platform v1.0"""

# TEL-C3-909 (docs/02_design.md §逐次報告 / §Read-only 診断の多層物理エンフォース):
# CASE 2 custom act node. `ToolActNode` (framework default) has no
# extension hook — its `execute()` is a single fixed method — so this class
# is a full override, not a hook-based extension. It intentionally
# reproduces the framework's HITL-policy / confirmation-pass structure
# (this template disables HITL — hitl.enabled: false — so that branch is a
# straight pass-through) and adds exactly one behavior on top: after each
# tool call executes, push one factual line to ChatOps (docs/02 format:
# "[TEL-C3-909] {target}: {step} → {verdict} | 確認済 N/M | 次候補: {next}").
#
# Why the report push is here and not left to the LLM: docs/02
# ユーザビリティ設計レビュー "逐次報告の確実性" — an LLM asked to "call a
# report tool" can forget; a node that always fires after every tool
# execution cannot skip it.
#
# S-1: INTERNAL (docs/02 §Security Model S-1) — full diagnostic execution
# (i.e. this node) is only permitted for INTERNAL callers (the monitoring
# webhook receiver). A VERIFIED_EXTERNAL-only invocation is rejected here by
# the S-1 gate in BaseNode.__call__ before execute() runs; TriageFinalizeNode
# still produces a valid degraded/no-diagnostics report in that case
# (status=error is preserved, not silently converted to success).

from __future__ import annotations

import time
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import current_secrets
from shared.utils.audit_logger import emit_trace_event

import httpx

_DEFAULT_WEBHOOK_SECRET_KEY = "CHATOPS_WEBHOOK_URL"


def _format_report_line(template_id: str, target: str, step: str, verdict: str, checked: int, total: int, next_step: str) -> str:
    return f"[{template_id}] {target}: {step} → {verdict} | 確認済 {checked}/{total} | 次候補: {next_step}"


class TriageActNode(FunctionNode):
    """Custom act node: executes read-only tool calls + pushes incremental ChatOps reports."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def __init__(self, tools: list, template_id: str = "TEL-C3-909", webhook_secret_key: str | None = None,
                 min_interval_seconds: float = 0.0, reporting_enabled: bool = True) -> None:
        self._tools_by_name: dict = {
            str(getattr(t, "name", None) or getattr(t, "__name__", repr(t))): t for t in tools
        }
        self._template_id = template_id
        self._webhook_secret_key = webhook_secret_key or _DEFAULT_WEBHOOK_SECRET_KEY
        self._min_interval_seconds = min_interval_seconds
        self._reporting_enabled = reporting_enabled

    # ------------------------------------------------------------------
    # S-2 / S-3 domain hooks — no domain-specific input/output checks
    # needed beyond the framework defaults for this node (tool_calls are
    # already validated at the DSL/tool-registry boundary; execute() below
    # rejects unknown tool names defensively regardless).
    # ------------------------------------------------------------------

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def _extra_security_gate_output(self, result: dict) -> dict:
        return result

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    def execute(self, state: AgentState) -> dict:
        tool_calls: list[dict] = state.get("tool_calls", []) or []
        if not tool_calls:
            return {"tool_calls": [], "tool_results": []}

        for tc in tool_calls:
            if tc.get("name", "") not in self._tools_by_name:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"[TriageActNode] unknown tool: {tc.get('name', '')!r}"],
                    "tool_calls": [],
                }

        working_memory = dict(state.get("working_memory", {}))
        visited: list[dict] = list(working_memory.get("visited", []))
        report_log: list[dict] = list(working_memory.get("report_log", []))
        hypotheses: list[dict] = list(working_memory.get("hypotheses", []))
        decision_tree = working_memory.get("decision_tree", {}) or {}
        total_nodes = len(decision_tree.get("nodes", [])) or max(len(visited) + len(tool_calls), 1)

        results: list[dict] = []
        last_push_ts = 0.0

        for tc in tool_calls:
            tool_name = tc.get("name", "")
            tool_args = tc.get("args", {}) or {}
            tool_fn = self._tools_by_name[tool_name]

            try:
                tool_result = tool_fn.invoke(tool_args) if hasattr(tool_fn, "invoke") else tool_fn(**tool_args)
            except Exception as exc:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"[TriageActNode] tool {tool_name!r} raised: {exc}"],
                    "tool_calls": [],
                    "tool_results": results,
                    "working_memory": {**working_memory, "visited": visited, "report_log": report_log},
                }

            results.append({"tool": tool_name, "result": tool_result, "args": tool_args})

            emit_trace_event(
                "diagnostic_executed",
                {"tool": tool_name, "target": tool_args.get("target", ""), "verdict": tool_result.get("status", "")
                 if isinstance(tool_result, dict) else ""},
                state,
            )

            if tool_name == "record_hypothesis" and isinstance(tool_result, dict):
                hypotheses.append(tool_result)
                continue  # no external reach, no visited entry, no report push

            target = tool_args.get("target", "unknown") if isinstance(tool_args, dict) else "unknown"
            verdict = tool_result.get("status", "unknown") if isinstance(tool_result, dict) else "unknown"
            # Prefer the DSL node id the LLM was instructed to echo back
            # (build_think_prompt() tells it to include dsl_node_id matching
            # the decision_tree node's own "id" field it is addressing) so
            # assemble_output()'s `unchecked` computation can compare against
            # actual decision_tree node ids. Falls back to a synthetic id
            # when dsl_node_id is absent (e.g. a tool call the LLM issued
            # outside the DSL, or a well-behaved-but-non-compliant LLM) —
            # such entries simply won't clear a DSL node from `unchecked`,
            # which is the safe (conservative) failure mode.
            dsl_node_id = tool_args.get("dsl_node_id") if isinstance(tool_args, dict) else None
            node_id = str(dsl_node_id) if dsl_node_id else f"{tool_name}:{target}:{len(visited)}"
            visited.append({
                "node_id": node_id,
                "target": target,
                "tool": tool_name,
                "result": tool_result if isinstance(tool_result, dict) else {},
                "verdict": verdict,
                "alert_ids": [a.get("alert_id") for a in working_memory.get("alerts", [])],
            })

            if self._reporting_enabled:
                now = time.monotonic()
                if now - last_push_ts >= self._min_interval_seconds:
                    checked = len(visited)
                    line = _format_report_line(
                        self._template_id, target, tool_name, verdict, checked, total_nodes, "—"
                    )
                    delivered, reason = self._push_report(line)
                    report_log.append({"line": line, "delivered": delivered, "reason": reason})
                    emit_trace_event("report_pushed", {"delivered": delivered, "line_len": len(line)}, state)
                    last_push_ts = now

        new_working_memory = {
            **working_memory,
            "visited": visited,
            "report_log": report_log,
            "hypotheses": hypotheses,
        }

        return {
            "tool_calls": [],
            "tool_results": results,
            "working_memory": new_working_memory,
        }

    # ------------------------------------------------------------------
    # ChatOps push (with 1 retry, never blocks the loop)
    # ------------------------------------------------------------------

    def _push_report(self, line: str) -> tuple[bool, str]:
        provider = current_secrets()
        try:
            webhook_url = provider.require(self._webhook_secret_key)
        except Exception as exc:
            return False, f"secret_unavailable: {exc}"

        for attempt in range(2):  # 1 try + 1 retry
            try:
                httpx.post(webhook_url, json={"text": line}, timeout=5.0)
                return True, ""
            except httpx.HTTPError as exc:
                if attempt == 1:
                    return False, str(exc)
        return False, "unreachable"
