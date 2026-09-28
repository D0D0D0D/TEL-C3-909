"""Shared test fixtures for TEL-C3-909."""

from __future__ import annotations

import json

import pytest


# tests/integration/test_graph.py:112-116 の happy path は追加の依存オブジェクトを必要としない。
@pytest.fixture
def agent_config():
    return {}


# tests/integration/test_graph.py:75-91,116 の _alertmanager_payload() と同じ入力形を使う。
@pytest.fixture
def case_payload():
    return json.dumps(
        {
            "groupKey": "x",
            "alerts": [
                {
                    "labels": {
                        "alertname": "HighLatency",
                        "service": "web-portal",
                        "severity": "critical",
                    },
                    "annotations": {"summary": "p99 latency high"},
                    "startsAt": "2026-07-06T00:00:00Z",
                }
            ],
        }
    )


# tests/integration/test_graph.py:116 の happy path は input_context を指定しないため空 dict を渡す。
@pytest.fixture
def live_input_context():
    return {}
