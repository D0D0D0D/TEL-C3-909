"""AgentCore Platform v1.0"""

# TEL-C3-909 (docs/02_design.md §Read-only 診断の多層物理エンフォース, layer ②
# "実行境界層"): a single protocol-agnostic egress gate. Every external
# reach a tool needs (HTTP GET/HEAD, TCP connect probe) goes through this
# module and nowhere else in the repo — there is no other code path that
# opens a socket or an HTTP connection.
#
# Physical enforcement, not config trust:
#   - HTTP: only GET and HEAD are implemented as callable code paths.
#     POST/PUT/DELETE/PATCH do not exist as functions in this module — an
#     LLM cannot "ask nicely" for a write method that was never written.
#   - TCP: connect-only handshake (open + immediately close). No API to
#     send application data exists here.
#   - Every destination is checked against `target_allowlist` (a list of
#     "host:port" strings from config) before any connection attempt.
#     An unregistered destination is rejected — never silently allowed.
#
# `target_allowlist` governance (docs/02_design.md, docs/07 operational
# rule): a target that cannot be issued a read-only credential MUST NOT be
# added to `target_allowlist` — see docs/07_operation_guide.md.

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Any

import httpx


class DestinationNotAllowedError(Exception):
    """Raised when a target is not present in the configured allowlist."""


@dataclass(frozen=True)
class GatewayResult:
    ok: bool
    status: str  # "ok" / "ng" / "failed" / "rejected"
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "detail": self.detail}


def _split_host_port(target: str) -> tuple[str, int]:
    if ":" not in target:
        raise ValueError(f"target must be 'host:port', got: {target!r}")
    host, _, port_s = target.rpartition(":")
    return host, int(port_s)


# Ports conventionally served over TLS. target_allowlist entries using one of
# these ports are addressed with https:// — plaintext HTTP to a TLS-only
# endpoint both fails the connection and (if it somehow succeeded) would send
# any Authorization header in clear text. This is a convention-based default,
# not a full scheme negotiation — operators needing a non-standard scheme/port
# combination should track this as a config extension point (see docs/07).
_TLS_PORTS = frozenset({443, 8443})


def _scheme_for_port(port: int) -> str:
    return "https" if port in _TLS_PORTS else "http"


class ReadonlyGateway:
    """Single egress point for all read-only diagnostic reach.

    Constructed once per invocation (or per tool call) with the
    `target_allowlist` resolved from config. All tool implementations in
    `src/tools/` MUST route external reach through this class — no other
    module may import `httpx`/`socket` for outbound connections.
    """

    def __init__(self, target_allowlist: list[str] | None = None, timeout_s: float = 5.0) -> None:
        self._allowlist: set[str] = set(target_allowlist or [])
        self._timeout_s = timeout_s

    def is_allowed(self, target: str) -> bool:
        return target in self._allowlist

    def _require_allowed(self, target: str) -> None:
        if not self.is_allowed(target):
            raise DestinationNotAllowedError(
                f"target {target!r} is not in target_allowlist — refusing to connect"
            )

    # ------------------------------------------------------------------
    # HTTP — GET / HEAD only. No POST/PUT/DELETE/PATCH code path exists.
    # ------------------------------------------------------------------

    def http_get(self, target: str, path: str = "/", credential: str | None = None) -> GatewayResult:
        """HTTP GET against an allowlisted target. Read-only by construction.

        `credential` (if provided) is sent as a Bearer Authorization header —
        this is the ONLY place the resolved DIAG_READONLY_CREDENTIALS value
        is used; it is never logged, never included in GatewayResult.detail,
        and never returned to the caller (layer ① — see diagnostic_tools.py).
        """
        try:
            self._require_allowed(target)
        except DestinationNotAllowedError as exc:
            return GatewayResult(ok=False, status="rejected", detail={"reason": str(exc)})

        _, port = _split_host_port(target)
        url = f"{_scheme_for_port(port)}://{target}{path}"
        headers = {"Authorization": f"Bearer {credential}"} if credential else None
        try:
            resp = httpx.get(url, timeout=self._timeout_s, headers=headers)
            ok = 200 <= resp.status_code < 400
            return GatewayResult(
                ok=ok,
                status="ok" if ok else "ng",
                detail={"http_status": resp.status_code, "url": url},
            )
        except httpx.HTTPError as exc:
            return GatewayResult(ok=False, status="failed", detail={"reason": str(exc), "url": url})

    def http_head(self, target: str, path: str = "/", credential: str | None = None) -> GatewayResult:
        """HTTP HEAD against an allowlisted target. Read-only by construction.

        See http_get() docstring re: `credential` handling.
        """
        try:
            self._require_allowed(target)
        except DestinationNotAllowedError as exc:
            return GatewayResult(ok=False, status="rejected", detail={"reason": str(exc)})

        _, port = _split_host_port(target)
        url = f"{_scheme_for_port(port)}://{target}{path}"
        headers = {"Authorization": f"Bearer {credential}"} if credential else None
        try:
            resp = httpx.head(url, timeout=self._timeout_s, headers=headers)
            ok = 200 <= resp.status_code < 400
            return GatewayResult(
                ok=ok,
                status="ok" if ok else "ng",
                detail={"http_status": resp.status_code, "url": url},
            )
        except httpx.HTTPError as exc:
            return GatewayResult(ok=False, status="failed", detail={"reason": str(exc), "url": url})

    # ------------------------------------------------------------------
    # TCP — connect-only probe. No API to send application data exists.
    # ------------------------------------------------------------------

    def tcp_connect(self, target: str) -> GatewayResult:
        """Open a TCP connection and immediately close it (connectivity probe only)."""
        try:
            self._require_allowed(target)
        except DestinationNotAllowedError as exc:
            return GatewayResult(ok=False, status="rejected", detail={"reason": str(exc)})

        host, port = _split_host_port(target)
        t0 = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=self._timeout_s):
                pass
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            return GatewayResult(ok=True, status="ok", detail={"target": target, "elapsed_ms": elapsed_ms})
        except OSError as exc:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            return GatewayResult(
                ok=False,
                status="failed",
                detail={"target": target, "reason": str(exc), "elapsed_ms": elapsed_ms},
            )
