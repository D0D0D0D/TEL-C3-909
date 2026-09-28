# TEL-C3-909 — Unit Tests: ReadonlyGateway (layer ② physical enforcement)

from unittest.mock import patch

import httpx

from src.services.readonly_gateway import ReadonlyGateway


class TestReadonlyGatewayAllowlist:
    def test_http_get_rejects_unregistered_target(self):
        gw = ReadonlyGateway(target_allowlist=["allowed-host:80"])
        result = gw.http_get("not-allowed:80", "/x")
        assert result.status == "rejected"
        assert result.ok is False

    def test_tcp_connect_rejects_unregistered_target(self):
        gw = ReadonlyGateway(target_allowlist=["allowed-host:80"])
        result = gw.tcp_connect("not-allowed:80")
        assert result.status == "rejected"

    def test_is_allowed(self):
        gw = ReadonlyGateway(target_allowlist=["a:1", "b:2"])
        assert gw.is_allowed("a:1") is True
        assert gw.is_allowed("c:3") is False

    def test_empty_allowlist_rejects_everything(self):
        gw = ReadonlyGateway(target_allowlist=[])
        assert gw.http_get("anything:80").status == "rejected"
        assert gw.tcp_connect("anything:80").status == "rejected"


class TestReadonlyGatewayNoWriteMethods:
    def test_no_write_http_methods_exist(self):
        """PB: write methods must not exist as callable code paths (layer ②)."""
        public_methods = {m for m in dir(ReadonlyGateway) if not m.startswith("_")}
        for forbidden in ("http_post", "http_put", "http_delete", "http_patch", "send"):
            assert forbidden not in public_methods

    def test_only_get_head_and_tcp_connect_present(self):
        public_methods = {m for m in dir(ReadonlyGateway) if not m.startswith("_")}
        assert public_methods == {"http_get", "http_head", "tcp_connect", "is_allowed"}


class TestReadonlyGatewayHttp:
    def test_http_get_success(self):
        gw = ReadonlyGateway(target_allowlist=["good:80"])
        with patch("src.services.readonly_gateway.httpx.get") as mock_get:
            mock_get.return_value = httpx.Response(200, request=httpx.Request("GET", "http://good:80/health"))
            result = gw.http_get("good:80", "/health")
        assert result.ok is True
        assert result.status == "ok"

    def test_http_get_non_2xx_is_ng(self):
        gw = ReadonlyGateway(target_allowlist=["good:80"])
        with patch("src.services.readonly_gateway.httpx.get") as mock_get:
            mock_get.return_value = httpx.Response(503, request=httpx.Request("GET", "http://good:80/health"))
            result = gw.http_get("good:80", "/health")
        assert result.ok is False
        assert result.status == "ng"

    def test_http_get_connection_error_is_failed(self):
        gw = ReadonlyGateway(target_allowlist=["good:80"])
        with patch("src.services.readonly_gateway.httpx.get", side_effect=httpx.ConnectError("boom")):
            result = gw.http_get("good:80", "/health")
        assert result.status == "failed"

    def test_http_head_success(self):
        gw = ReadonlyGateway(target_allowlist=["good:80"])
        with patch("src.services.readonly_gateway.httpx.head") as mock_head:
            mock_head.return_value = httpx.Response(200, request=httpx.Request("HEAD", "http://good:80/"))
            result = gw.http_head("good:80")
        assert result.ok is True


class TestReadonlyGatewayTcp:
    def test_tcp_connect_success(self):
        gw = ReadonlyGateway(target_allowlist=["good:80"])
        with patch("src.services.readonly_gateway.socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = lambda self: self
            mock_conn.return_value.__exit__ = lambda self, *a: None
            result = gw.tcp_connect("good:80")
        assert result.ok is True

    def test_tcp_connect_failure(self):
        gw = ReadonlyGateway(target_allowlist=["good:80"])
        with patch("src.services.readonly_gateway.socket.create_connection", side_effect=OSError("refused")):
            result = gw.tcp_connect("good:80")
        assert result.ok is False
        assert result.status == "failed"
