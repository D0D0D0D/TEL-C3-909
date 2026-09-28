"""AgentCore Platform v1.0"""

# TEL-C3-909 (docs/02_design.md §Data Flow / §ユーザビリティ設計レビュー "入力の自然さ"):
# normalizes raw monitoring-system webhook payloads into the agent's
# canonical alert shape so customers can point their existing Alertmanager /
# Datadog / PagerDuty webhook directly at this agent without building a
# translation layer first.
#
# Canonical alert shape (see src/schemas/state.py working_memory.alerts):
#   {alert_id, target_system, alert_type, severity, ts, metrics}
#
# All adapters here are pure functions — no I/O, no side effects, no
# credentials. They are called from TriageInitializeNode.on_initialize()
# after the raw webhook JSON has been parsed from `user_input`.

from __future__ import annotations

from typing import Any


def _safe_get(d: dict, *path: str, default: Any = None) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def adapt_alertmanager(payload: dict) -> list[dict]:
    """Prometheus Alertmanager webhook payload -> canonical alerts.

    Alertmanager groups correlated alerts into a single payload under
    `alerts`, which maps directly onto docs/02's "同一 group payload 内共有"
    v0 scope for correlated-alert history sharing.
    """
    alerts_raw = payload.get("alerts") or []
    out: list[dict] = []
    for a in alerts_raw:
        labels = a.get("labels", {}) if isinstance(a, dict) else {}
        annotations = a.get("annotations", {}) if isinstance(a, dict) else {}
        out.append(
            {
                "alert_id": str(labels.get("alertname", "") or a.get("fingerprint", "")),
                "target_system": str(labels.get("service") or labels.get("instance") or "unknown"),
                "alert_type": str(labels.get("alertname", "unknown")),
                "severity": str(labels.get("severity", "unknown")),
                "ts": str(a.get("startsAt", "")),
                "metrics": {"summary": str(annotations.get("summary", ""))},
            }
        )
    return out


def adapt_datadog(payload: dict) -> list[dict]:
    """Datadog monitor webhook payload -> canonical alerts."""
    return [
        {
            "alert_id": str(payload.get("id", "") or payload.get("alert_id", "")),
            "target_system": str(payload.get("host", "") or payload.get("scope", "unknown")),
            "alert_type": str(payload.get("alert_type", "unknown")),
            "severity": str(payload.get("alert_priority", payload.get("priority", "unknown"))),
            "ts": str(payload.get("date", "")),
            "metrics": {"title": str(payload.get("title", "")), "text": str(payload.get("text", ""))[:500]},
        }
    ]


def adapt_pagerduty(payload: dict) -> list[dict]:
    """PagerDuty Events webhook (v3) payload -> canonical alerts."""
    messages = payload.get("messages") or [payload]
    out: list[dict] = []
    for m in messages:
        incident = _safe_get(m, "incident", default={}) or m
        out.append(
            {
                "alert_id": str(_safe_get(incident, "id", default="") or ""),
                "target_system": str(_safe_get(incident, "service", "summary", default="unknown")),
                "alert_type": str(_safe_get(incident, "type", default="incident")),
                "severity": str(_safe_get(incident, "urgency", default="unknown")),
                "ts": str(_safe_get(incident, "created_at", default="")),
                "metrics": {"title": str(_safe_get(incident, "title", default=""))},
            }
        )
    return out


def adapt_generic(payload: dict) -> list[dict]:
    """Fallback mapping for monitoring systems without a named adapter.

    Requires the payload to already carry roughly the canonical field names
    (or a single top-level alert dict) — the customer configures this
    mapping once at onboarding time.
    """
    if "alerts" in payload and isinstance(payload["alerts"], list):
        raw_list = payload["alerts"]
    else:
        raw_list = [payload]

    out: list[dict] = []
    for a in raw_list:
        if not isinstance(a, dict):
            continue
        out.append(
            {
                "alert_id": str(a.get("alert_id", "")),
                "target_system": str(a.get("target_system", "unknown")),
                "alert_type": str(a.get("alert_type", "unknown")),
                "severity": str(a.get("severity", "unknown")),
                "ts": str(a.get("ts", "")),
                "metrics": a.get("metrics", {}) if isinstance(a.get("metrics"), dict) else {},
            }
        )
    return out


_ADAPTERS = {
    "alertmanager": adapt_alertmanager,
    "datadog": adapt_datadog,
    "pagerduty": adapt_pagerduty,
    "generic": adapt_generic,
}


def detect_adapter(payload: dict, enabled_adapters: list[str]) -> str:
    """Best-effort detection of which webhook format this payload is.

    Detection is heuristic and only considers adapters enabled in config
    (`input_adapters`). Falls back to "generic" (or the first enabled
    adapter) when no signature matches.
    """
    if "alertmanager" in enabled_adapters and "groupKey" in payload and "alerts" in payload:
        return "alertmanager"
    if "datadog" in enabled_adapters and ("alert_type" in payload or "alert_transition" in payload):
        return "datadog"
    if "pagerduty" in enabled_adapters and ("messages" in payload or "incident" in payload):
        return "pagerduty"
    if "generic" in enabled_adapters:
        return "generic"
    return enabled_adapters[0] if enabled_adapters else "generic"


def normalize_alerts(payload: dict, enabled_adapters: list[str] | None = None) -> tuple[list[dict], str]:
    """Normalize a raw webhook payload into canonical alerts.

    Returns (alerts, adapter_name_used).
    """
    enabled = enabled_adapters or list(_ADAPTERS.keys())
    adapter_name = detect_adapter(payload, enabled)
    adapter_fn = _ADAPTERS.get(adapter_name, adapt_generic)
    return adapter_fn(payload), adapter_name
