"""AgentCore Platform v1.0"""

# TEL-C3-909 (docs/02_design.md §Tools): the agent's entire action space.
# get_tools() in src/graph/graph.py returns exactly these 6 callables —
# there is no other tool an LLM can invoke (layer ③ enforcement, docs/02
# §Read-only 診断の多層物理エンフォース). Every tool except
# `record_hypothesis` routes external reach through `ReadonlyGateway`
# (layer ②) and resolves connection credentials via `ctx.secrets.require()`
# (layer ①) — never from State, never from runbook text.
#
# Tool return values are msgpack-safe dicts (they are appended to
# `tool_results`, which is checkpointed).

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from framework.secrets.context import current_secrets
from shared.security.credential_detector import detect_credentials
from shared.security.pii_detector import detect_pii

from src.services.readonly_gateway import ReadonlyGateway

_READONLY_CRED_SECRET_KEY = "DIAG_READONLY_CREDENTIALS"


def _scrub(text: str) -> str:
    findings = list(detect_credentials(text)) + list(detect_pii(text))
    if not findings:
        return text
    findings.sort(key=lambda f: f["start"])
    merged: list[dict[str, Any]] = []
    for f in findings:
        if merged and f["start"] < merged[-1]["end"]:
            continue
        merged.append(f)
    scrubbed = text
    for f in reversed(merged):
        scrubbed = scrubbed[: f["start"]] + "[REDACTED]" + scrubbed[f["end"] :]
    return scrubbed


def _resolve_readonly_credential(target: str) -> str | None:
    """Resolve the read-only credential for `target` from ctx.secrets.

    DIAG_READONLY_CREDENTIALS is expected to be a per-target credential
    reference (opaque to this tool layer — the concrete lookup/parsing is
    an integration detail of the credential store). Never sourced from
    runbook text or State (docs/02_design.md State Constraints).
    """
    provider = current_secrets()
    try:
        raw = provider.require(_READONLY_CRED_SECRET_KEY)
    except Exception:
        return None
    # The credential value itself is intentionally not echoed anywhere —
    # tools only use it to authenticate; it never appears in a return value.
    return raw


def build_diagnostic_tools(gateway: ReadonlyGateway) -> list:
    """Build the 6 read-only tool callables bound to a ReadonlyGateway instance.

    Returns LangChain-compatible `@tool`-decorated callables (per SDK
    how-to: build-autonomous-agent.md) so ThinkNode can bind_tools() them.
    """

    @tool
    def http_get_healthcheck(target: str, dsl_node_id: str = "") -> dict:
        """Perform an HTTP GET against an approved health-check endpoint.
        200-399 status codes are treated as healthy. `target` must be a
        'host:port' string already present in target_allowlist.
        `dsl_node_id` MUST be set to the decision_tree node's own "id" field
        for the check being performed — it is used to record which DSL node
        this observation corresponds to."""
        credential = _resolve_readonly_credential(target)
        result = gateway.http_get(target, path="/health", credential=credential)
        return {"target": target, **result.to_dict()}

    @tool
    def probe_connectivity(target: str, dsl_node_id: str = "") -> dict:
        """Probe TCP connectivity to `target` (connect + close only, no
        application data sent). Verifies basic network reachability.
        `dsl_node_id` MUST be set to the decision_tree node's own "id" field
        for the check being performed."""
        result = gateway.tcp_connect(target)
        return {"target": target, **result.to_dict()}

    @tool
    def read_metric(target: str, metric: str, window: str = "5m", dsl_node_id: str = "") -> dict:
        """Read a single metric value from the monitoring API at `target`
        over the given time window (GET only).
        `dsl_node_id` MUST be set to the decision_tree node's own "id" field
        for the check being performed."""
        credential = _resolve_readonly_credential(target)
        result = gateway.http_get(target, path=f"/metrics/{metric}?window={window}", credential=credential)
        return {"target": target, "metric": metric, "window": window, **result.to_dict()}

    @tool
    def tail_log(target: str, lines: int = 20, dsl_node_id: str = "") -> dict:
        """Read the last `lines` of the read-only log endpoint at `target`.
        Credential- and PII-scrubs the returned text before it is placed
        into tool_results (which is checkpointed).
        `dsl_node_id` MUST be set to the decision_tree node's own "id" field
        for the check being performed."""
        credential = _resolve_readonly_credential(target)
        result = gateway.http_get(target, path=f"/logs/tail?lines={lines}", credential=credential)
        detail = dict(result.detail)
        # If a real backend echoed log text in detail (future extension),
        # scrub it before it ever reaches tool_results / checkpoint.
        if "log_text" in detail and isinstance(detail["log_text"], str):
            detail["log_text"] = _scrub(detail["log_text"])
        return {"target": target, "lines": lines, "ok": result.ok, "status": result.status, "detail": detail}

    @tool
    def check_liveness(target: str, dsl_node_id: str = "") -> dict:
        """Check liveness / login-screen reachability at `target` (GET only —
        never actually authenticates).
        `dsl_node_id` MUST be set to the decision_tree node's own "id" field
        for the check being performed."""
        credential = _resolve_readonly_credential(target)
        result = gateway.http_get(target, path="/liveness", credential=credential)
        return {"target": target, **result.to_dict()}

    @tool
    def record_hypothesis(cause: str, confidence: float, evidence: list[str]) -> dict:
        """Record a root-cause hypothesis with a confidence score (0.0-1.0)
        and evidence references. The ONLY tool with no external reach —
        writes into working_memory.hypotheses only. `cause` must be phrased
        as a hypothesis, never an assertive claim. `evidence` must be a
        list of node_id references (never raw log/connection data)."""
        return {
            "cause": str(cause),
            "confidence": float(confidence),
            "evidence": [str(e) for e in evidence],
        }

    return [
        http_get_healthcheck,
        probe_connectivity,
        read_metric,
        tail_log,
        check_liveness,
        record_hypothesis,
    ]
