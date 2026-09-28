# PB-3: L1 -> External service — real (mocked-at-the-transport-layer)
# connection through the framework's node/tool boundary to an external
# read-only diagnostic endpoint via ReadonlyGateway.
#
# TEL-C3-909 has no always-available external test system, so this PB test
# verifies the full connection path down to the actual httpx/socket call
# boundary (transport itself mocked, matching CI's no-network policy) while
# proving every layer above it — tool -> gateway -> httpx/socket — is wired
# correctly end-to-end, not stubbed out earlier in the chain.

from unittest.mock import patch

import httpx

from src.services.readonly_gateway import ReadonlyGateway
from src.tools.diagnostic_tools import build_diagnostic_tools


class TestPB3ExternalServiceConnection:
    def test_http_get_healthcheck_reaches_httpx_transport(self):
        gw = ReadonlyGateway(target_allowlist=["portal-health:443"])
        tools = {t.name: t for t in build_diagnostic_tools(gw)}

        with patch("src.services.readonly_gateway.httpx.get") as mock_get:
            mock_get.return_value = httpx.Response(
                200, request=httpx.Request("GET", "http://portal-health:443/health")
            )
            result = tools["http_get_healthcheck"].invoke({"target": "portal-health:443"})

        mock_get.assert_called_once()
        called_url = mock_get.call_args[0][0]
        # Port 443 maps to https:// (see readonly_gateway._scheme_for_port) —
        # plaintext HTTP to a TLS-conventional port both fails the connection
        # and would leak the Authorization header in clear text if it somehow
        # succeeded.
        assert called_url == "https://portal-health:443/health"
        assert result["ok"] is True

    def test_http_get_healthcheck_non_tls_port_uses_http(self):
        gw = ReadonlyGateway(target_allowlist=["portal-health:8080"])
        tools = {t.name: t for t in build_diagnostic_tools(gw)}

        with patch("src.services.readonly_gateway.httpx.get") as mock_get:
            mock_get.return_value = httpx.Response(
                200, request=httpx.Request("GET", "http://portal-health:8080/health")
            )
            tools["http_get_healthcheck"].invoke({"target": "portal-health:8080"})

        called_url = mock_get.call_args[0][0]
        assert called_url == "http://portal-health:8080/health"

    def test_http_get_healthcheck_sends_resolved_credential_as_bearer_header(self):
        from framework.secrets.context import bound_secrets
        from shared.secrets.inmemory_provider import InMemoryProvider

        gw = ReadonlyGateway(target_allowlist=["portal-health:443"])
        tools = {t.name: t for t in build_diagnostic_tools(gw)}

        with patch("src.services.readonly_gateway.httpx.get") as mock_get, bound_secrets(
            InMemoryProvider({"DIAG_READONLY_CREDENTIALS": "mock-readonly-cred-for-testing"})
        ):
            mock_get.return_value = httpx.Response(
                200, request=httpx.Request("GET", "https://portal-health:443/health")
            )
            tools["http_get_healthcheck"].invoke({"target": "portal-health:443"})

        _, kwargs = mock_get.call_args
        assert kwargs["headers"] == {"Authorization": "Bearer mock-readonly-cred-for-testing"}

    def test_probe_connectivity_reaches_socket_layer(self):
        gw = ReadonlyGateway(target_allowlist=["db-host:5432"])
        tools = {t.name: t for t in build_diagnostic_tools(gw)}

        with patch("src.services.readonly_gateway.socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = lambda self: self
            mock_conn.return_value.__exit__ = lambda self, *a: None
            result = tools["probe_connectivity"].invoke({"target": "db-host:5432"})

        mock_conn.assert_called_once_with(("db-host", 5432), timeout=5.0)
        assert result["ok"] is True
