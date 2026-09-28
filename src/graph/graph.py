"""AgentCore Platform v1.0"""

# TEL-C3-909 — MonitoringAlertFirstTriageAgent (Cat 3 / TEL).
#
# docs/02_design.md is the implementation source of truth. Summary:
#
#   L1 base    : AutonomousBaseGraph (Cat 3 — autonomous think/act loop;
#                AgentBaseGraph's fixed pipeline cannot express
#                observation-driven branch selection or self-directed
#                termination).
#   Pipeline   : START → initialize → think ⇄ act → finalize → END
#                (framework-owned; add_edges() / _route_after_think() are
#                NOT overridden here).
#   register_nodes(): CASE 2 — super() wires all four slots first, then
#                initialize/act/finalize are replaced with custom nodes.
#                `think` is left as the framework default `ThinkNode`; the
#                triage strategy (which branch to check next, when to stop)
#                lives entirely in build_think_prompt(), not in code, so
#                the agent — not a fixed pipeline — owns termination
#                (Cat 3 requirement).
#
# assemble_output() known issue (docs/02 §Node Configuration): the released
# wheel never calls AutonomousBaseGraph.assemble_output() on its own —
# FinalizeNode.on_finalize() defaults to {}. TriageFinalizeNode.on_finalize()
# calls self._agent.assemble_output(state) explicitly (Enterprise Deep Research & Long-Form Report Generation Agent precedent)
# and preserves prior error/cancelled status instead of forcing SUCCESS.

from framework.graph.autonomous_base_graph import AutonomousBaseGraph

from src.nodes.triage_act_node import TriageActNode
from src.nodes.triage_finalize_node import TriageFinalizeNode
from src.nodes.triage_initialize_node import TriageInitializeNode
from src.schemas.state import State
from src.services.readonly_gateway import ReadonlyGateway
from src.tools.diagnostic_tools import build_diagnostic_tools

_DEFAULT_MAX_ITERATIONS = 24  # docs/02 config: decision-tree depth + retry margin


class MonitoringAlertFirstTriageAgent(AutonomousBaseGraph):
    """Cat 3 autonomous first-triage agent for monitoring alerts (TEL-C3-909).

    See docs/02_design.md for the full design (Runbook DSL, security model,
    incremental reporting, final output shape).
    """

    DEFAULT_MAX_ITERATIONS = _DEFAULT_MAX_ITERATIONS

    @property
    def name(self) -> str:
        return "tel_c3_909"

    @property
    def state_schema(self) -> type:
        return State

    # ------------------------------------------------------------------
    # Required abstract surface (AutonomousBaseGraph)
    # ------------------------------------------------------------------

    @property
    def max_iterations(self) -> int:
        return int(self.config.get("max_iterations", self.DEFAULT_MAX_ITERATIONS))

    def get_tools(self) -> list:
        target_allowlist = self.config.get("target_allowlist") or []
        gateway = ReadonlyGateway(target_allowlist=target_allowlist)
        return build_diagnostic_tools(gateway)

    def build_think_prompt(self, state) -> str:
        working_memory = state.get("working_memory", {}) or {}
        decision_tree = working_memory.get("decision_tree", {}) or {}
        visited = working_memory.get("visited", []) or []
        hypotheses = working_memory.get("hypotheses", []) or []
        degraded_mode = working_memory.get("degraded_mode", False)
        confidence_threshold = self.config.get("confidence_threshold", 0.75)
        iterations = state.get("iterations", 0)
        cost_usd = state.get("cost_usd", 0.0)
        budget_usd = self.config.get("budget_usd")

        if degraded_mode or not decision_tree.get("nodes"):
            degraded_reason = working_memory.get("degraded_reason", "")
            if degraded_reason == "insufficient_trust_for_diagnostics":
                reason_text = (
                    "呼び出し元の trust level が診断実行に必要な INTERNAL ではありません。"
                    "診断（tool_calls）は一切発行できません。"
                )
            else:
                reason_text = "Runbook が未整備、または取得/マッチに失敗しました。診断は実行できません。"
            return (
                "監視アラート一次切り分けエージェント。縮退モードです。"
                f"{reason_text}\n"
                "record_hypothesis を含め、いかなる tool も絶対に呼び出さないでください"
                "（tool_calls は必ず空のままにしてください）。"
                "status=success として、受信・対象特定・縮退理由のみを報告する最終判断をしてください。\n"
                f"縮退理由コード: {degraded_reason or 'unknown'}\n"
                f"アラート: {working_memory.get('alerts', [])}"
            )

        visited_node_ids = {v.get("node_id", "") for v in visited}
        all_node_ids = {n.get("id") for n in decision_tree.get("nodes", [])}
        unvisited = sorted(all_node_ids - visited_node_ids)

        max_confidence = max((h.get("confidence", 0.0) for h in hypotheses), default=0.0)

        return (
            "あなたは監視アラート一次切り分け（TEL-C3-909）の自律エージェントです。"
            "以下の決定木DSLの範囲内でのみ、read-onlyの確認（tool_calls）を発行できます。\n\n"
            f"決定木: {decision_tree}\n"
            f"確認済み(visited): {visited}\n"
            f"未確認ノード候補: {unvisited}\n"
            f"現在の仮説(hypotheses): {hypotheses}（最大確度 {max_confidence:.2f}）\n"
            f"確度しきい値(confidence_threshold): {confidence_threshold}\n"
            f"反復回数: {iterations}/{self.max_iterations} | 累積コスト: {cost_usd} / budget {budget_usd}\n\n"
            "行動原則:\n"
            "1. 異常が観測された枝を優先して深掘りする（決定木の on_ng 側）。\n"
            "2. 確認が失敗(failed)した場合は (a) 1回だけ再試行 (b) 別系統の確認へジャンプ "
            "(c) その症状自体を根拠に仮説を更新、のいずれかを理由付きで選ぶ。\n"
            "3. 仮説を更新するたびに record_hypothesis tool を呼び、根拠(evidence)は "
            "visited の node_id のみを参照する（生ログ・接続情報は書かない）。\n"
            "4. 決定木を辿り切った、または最大確度が confidence_threshold 以上に達したら、"
            "tool_calls を発行せず status=success として終了する。\n"
            "5. allowlist 外の操作が必要と判断した場合は診断を打ち切り、"
            "working_memory の escalation_required 相当の判断を仮説/理由に明記する。\n"
            "6. 診断ツール（record_hypothesis 以外）を呼ぶ際は、必ず args に "
            "dsl_node_id として決定木ノードの id フィールドの値をそのまま含めること"
            "（例: 決定木ノード id='check-health-ep' を実行するなら "
            "dsl_node_id='check-health-ep'）。これにより実施済み確認が正しく記録される。\n"
        )

    def assemble_output(self, state) -> dict:
        working_memory = state.get("working_memory", {}) or {}
        alerts = working_memory.get("alerts", []) or []
        visited = working_memory.get("visited", []) or []
        decision_tree = working_memory.get("decision_tree", {}) or {}
        hypotheses = working_memory.get("hypotheses", []) or []
        runbook_ref = working_memory.get("runbook_ref", {}) or {}
        report_log = working_memory.get("report_log", []) or []
        degraded_mode = working_memory.get("degraded_mode", False)
        degraded_reason = working_memory.get("degraded_reason", "")
        status = state.get("status", "pending")

        # visited[].node_id is the actual DSL node id (str(dsl_node_id) — see
        # TriageActNode) when the LLM followed the build_think_prompt()
        # instruction to echo it back via tool args; comparing full node_id
        # values (not a ":"-split prefix — the DSL's own ids may legitimately
        # contain ":" or "-") against decision_tree node ids is therefore the
        # correct match, not a prefix heuristic.
        all_node_ids = [n.get("id") for n in decision_tree.get("nodes", [])]
        checked_ids = {v.get("node_id", "") for v in visited}
        unchecked = [nid for nid in all_node_ids if nid not in checked_ids]

        top_hypothesis = max(hypotheses, key=lambda h: h.get("confidence", 0.0), default=None)
        confidence_threshold = self.config.get("confidence_threshold", 0.75)

        escalation_required = bool(working_memory.get("escalation_required", False))
        # A cancelled/error/degraded run always needs human follow-up.
        if status in ("error", "cancelled") or degraded_mode:
            escalation_required = True

        recommended_actions: list[str] = []
        if degraded_reason == "insufficient_trust_for_diagnostics":
            recommended_actions.append(
                "呼び出し元の trust level が INTERNAL でないため診断は未実施です。"
                "深掘り調査が必要な場合は監視 webhook receiver 経由で再発火してください"
                "（docs/07_operation_guide.md 参照）。人間によるエスカレーションが必要です。"
            )
        elif degraded_mode:
            recommended_actions.append(
                "Runbook 未整備または取得/マッチ失敗のため自動診断は未実施です。"
                "人間による一次確認とエスカレーションが必要です。"
            )
        elif top_hypothesis and top_hypothesis.get("confidence", 0.0) >= confidence_threshold:
            recommended_actions.append(
                f"最有力仮説（確度 {top_hypothesis.get('confidence', 0.0):.2f}）に基づく対処の実行判断は "
                "人間（NOC/SRE）が行ってください。"
            )
        else:
            recommended_actions.append("確度が閾値未満のため、追加確認は人間による再トリアージが必要です。")

        if escalation_required:
            recommended_actions.append(
                "allowlist 外の操作、または打ち切りにより、エスカレーション（人間対応）が必要です。"
            )

        if status == "cancelled":
            summary_status = f"一次切り分け 打ち切り（budget 到達）/ 確認 {len(visited)}/{len(all_node_ids) or '?'}"
        elif status == "error":
            summary_status = f"一次切り分け 実行不可（診断は INTERNAL 経路のみ）/ 確認 {len(visited)}/{len(all_node_ids) or '?'}"
        elif degraded_reason == "insufficient_trust_for_diagnostics":
            summary_status = "一次切り分け 縮退動作（診断は INTERNAL 経路のみ・trust level 不足）"
        elif degraded_mode:
            summary_status = "一次切り分け 縮退動作（Runbook 未整備）"
        else:
            summary_status = f"一次切り分け 完了 / 確認 {len(visited)}/{len(all_node_ids) or '?'}"

        top_hypothesis_line = (
            f"{top_hypothesis.get('cause')}（確度 {top_hypothesis.get('confidence', 0.0):.2f}）"
            if top_hypothesis
            else "仮説なし"
        )
        next_action_line = (
            f"{recommended_actions[0]} (escalation_required: {escalation_required})"
            if recommended_actions
            else f"(escalation_required: {escalation_required})"
        )

        summary = {
            "status": summary_status,
            "top_hypothesis": top_hypothesis_line,
            "next_action": next_action_line,
            "escalation_required": escalation_required,
        }

        incident_report_draft = (
            f"# インシデント一次切り分け報告\n\n"
            f"- 状況: {summary_status}\n"
            f"- 対象アラート: {[a.get('alert_id') for a in alerts]}\n"
            f"- 最有力仮説: {top_hypothesis_line}\n"
            f"- 実施した確認: {len(visited)} 件 / 未確認: {len(unchecked)} 件\n"
            f"- 推奨対処: {'; '.join(recommended_actions)}\n"
        )

        return {
            "summary": summary,
            "checked_matrix": visited,
            "unchecked": unchecked,
            "hypotheses": hypotheses,
            "recommended_actions": recommended_actions,
            "incident_report_draft": incident_report_draft,
            "report_log": report_log,
            "runbook_trace": {**runbook_ref, "decision_tree": decision_tree},
            "escalation_required": escalation_required,
        }

    # ------------------------------------------------------------------
    # CASE 2 register_nodes() — replace initialize/act/finalize; think stays default
    # ------------------------------------------------------------------

    def register_nodes(self) -> None:
        super().register_nodes()  # wires initialize/think/act/finalize (framework defaults)

        reporting = self.config.get("reporting", {}) or {}
        tools = self.get_tools()

        self._nodes["initialize"] = TriageInitializeNode(self)
        self._nodes["act"] = TriageActNode(
            tools,
            template_id=self.config.get("agent_id", "TEL-C3-909"),
            webhook_secret_key=reporting.get("webhook_secret_key", "CHATOPS_WEBHOOK_URL"),
            min_interval_seconds=reporting.get("min_interval_seconds", 0),
            reporting_enabled=reporting.get("enabled", True),
        )
        self._nodes["finalize"] = TriageFinalizeNode(self)
        # "think" intentionally left as the framework default ThinkNode —
        # the triage strategy lives in build_think_prompt(), not in a
        # custom node (Cat 3 requirement: agent owns termination judgment).


# Backward-compatible alias (scaffold callers import `Graph`)
Graph = MonitoringAlertFirstTriageAgent
