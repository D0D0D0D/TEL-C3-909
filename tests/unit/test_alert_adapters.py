# TEL-C3-909 — Unit Tests: alert_adapters (webhook payload normalization)

from src.services.alert_adapters import (
    adapt_alertmanager,
    adapt_datadog,
    adapt_generic,
    adapt_pagerduty,
    detect_adapter,
    normalize_alerts,
)


class TestAdaptAlertmanager:
    def test_basic_group_payload(self):
        payload = {
            "groupKey": "x",
            "alerts": [
                {
                    "labels": {"alertname": "HighLatency", "service": "web-portal", "severity": "critical"},
                    "annotations": {"summary": "p99 high"},
                    "startsAt": "2026-07-06T00:00:00Z",
                }
            ],
        }
        alerts = adapt_alertmanager(payload)
        assert len(alerts) == 1
        assert alerts[0]["target_system"] == "web-portal"
        assert alerts[0]["alert_type"] == "HighLatency"
        assert alerts[0]["severity"] == "critical"

    def test_empty_alerts_list(self):
        assert adapt_alertmanager({"alerts": []}) == []

    def test_multiple_correlated_alerts(self):
        payload = {"alerts": [{"labels": {"alertname": "A", "service": "s1"}}, {"labels": {"alertname": "B", "service": "s2"}}]}
        alerts = adapt_alertmanager(payload)
        assert len(alerts) == 2


class TestAdaptDatadog:
    def test_basic_payload(self):
        payload = {"id": "123", "host": "web-portal", "alert_type": "error", "alert_priority": "P1", "date": "1234", "title": "t", "text": "x"}
        alerts = adapt_datadog(payload)
        assert len(alerts) == 1
        assert alerts[0]["target_system"] == "web-portal"


class TestAdaptPagerduty:
    def test_messages_wrapper(self):
        payload = {"messages": [{"incident": {"id": "1", "service": "s", "type": "incident", "urgency": "high", "created_at": "t", "title": "T"}}]}
        alerts = adapt_pagerduty(payload)
        assert len(alerts) == 1
        assert alerts[0]["alert_id"] == "1"

    def test_bare_incident(self):
        payload = {"id": "2", "service": "s2"}
        alerts = adapt_pagerduty(payload)
        assert len(alerts) == 1


class TestAdaptGeneric:
    def test_alerts_list(self):
        payload = {"alerts": [{"alert_id": "a1", "target_system": "sys1", "alert_type": "t", "severity": "s", "ts": "1"}]}
        alerts = adapt_generic(payload)
        assert alerts[0]["target_system"] == "sys1"

    def test_single_alert_dict(self):
        payload = {"alert_id": "a1", "target_system": "sys1"}
        alerts = adapt_generic(payload)
        assert len(alerts) == 1


class TestDetectAdapter:
    def test_detects_alertmanager(self):
        payload = {"groupKey": "x", "alerts": []}
        assert detect_adapter(payload, ["alertmanager", "datadog", "generic"]) == "alertmanager"

    def test_detects_datadog(self):
        payload = {"alert_type": "error"}
        assert detect_adapter(payload, ["alertmanager", "datadog", "generic"]) == "datadog"

    def test_detects_pagerduty(self):
        payload = {"messages": []}
        assert detect_adapter(payload, ["pagerduty", "generic"]) == "pagerduty"

    def test_falls_back_to_generic(self):
        payload = {"unknown_shape": True}
        assert detect_adapter(payload, ["alertmanager", "generic"]) == "generic"

    def test_respects_enabled_adapters_only(self):
        payload = {"groupKey": "x", "alerts": []}
        # alertmanager not enabled -> should not be detected even though shape matches
        result = detect_adapter(payload, ["datadog", "generic"])
        assert result != "alertmanager"


class TestNormalizeAlerts:
    def test_normalize_returns_adapter_used(self):
        payload = {"groupKey": "x", "alerts": [{"labels": {"alertname": "A", "service": "s"}}]}
        alerts, adapter_used = normalize_alerts(payload)
        assert adapter_used == "alertmanager"
        assert len(alerts) == 1
