# PART: entry-adapter v0.3.0 (parts@45f8315)
"""Authenticate entry callers and resolve trusted scope.

Client headers are never read for scope. Scope comes only from verified request
state (or the injected ``app.state.verify_scope`` seam), with a token-gated STG
default fallback for authenticated entry calls.

Scope contract (v0.3.0): the template declares its own KB partition key names
in ``SCOPE_KEYS`` (domain vocabulary, e.g. ``("production_company_id",
"project_id")``). ``authenticate_scope`` returns the verified scope as a dict
keyed by those names; every declared key must resolve to a clean non-empty
string or the request is rejected (fail closed).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping

from fastapi import HTTPException, Request
from framework.schemas.trust_level import TrustLevel


# Adaptation point: declare the template's KB partition keys in domain
# vocabulary. Each key doubles as the verified request.state attribute name
# the auth middleware stamps.
SCOPE_KEYS: tuple[str, ...] = ("production_company_id", "project_id")

# Adaptation point: STG default env var per scope key (used only after
# entry-token authentication).
STG_DEFAULT_SCOPE_ENVS: dict[str, str] = {key: f"STG_DEFAULT_{key.upper()}" for key in SCOPE_KEYS}


def authenticate_scope(request: Request) -> tuple[TrustLevel, dict[str, str]]:
    """Authenticate the caller and return its verified trust and scope dict."""
    _validate_scope_config()
    trust, token_authenticated = _resolve_trust(request)
    _require_verified_trust(trust)
    scope = _require_scope(request, token_authenticated=token_authenticated)
    return trust, scope


def _validate_scope_config() -> None:
    """Fail loudly on a misconfigured SCOPE_KEYS declaration (deployment error)."""
    if not isinstance(SCOPE_KEYS, tuple) or not SCOPE_KEYS:
        raise RuntimeError("entry_adapter: SCOPE_KEYS must be a non-empty tuple of key names")
    for key in SCOPE_KEYS:
        if not isinstance(key, str) or not key or key != key.strip() or not key.isidentifier():
            raise RuntimeError(f"entry_adapter: SCOPE_KEYS entries must be identifier strings: {SCOPE_KEYS!r}")
    if len(set(SCOPE_KEYS)) != len(SCOPE_KEYS):
        raise RuntimeError(f"entry_adapter: SCOPE_KEYS contains duplicates: {SCOPE_KEYS!r}")
    if not isinstance(STG_DEFAULT_SCOPE_ENVS, Mapping) or set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError(
            "entry_adapter: STG_DEFAULT_SCOPE_ENVS must be a mapping whose keys exactly match SCOPE_KEYS"
        )
    for env_name in STG_DEFAULT_SCOPE_ENVS.values():
        if (
            not isinstance(env_name, str)
            or not env_name
            or env_name != env_name.strip()
            or "\n" in env_name
            or "\r" in env_name
        ):
            raise RuntimeError(
                f"entry_adapter: STG_DEFAULT_SCOPE_ENVS values must be clean env var names: "
                f"{STG_DEFAULT_SCOPE_ENVS!r}"
            )


def _coerce_trust(value) -> TrustLevel:
    """Normalize a request.state.trust_level to a TrustLevel enum.

    Middleware may stamp the attribute as the enum OR as its raw string value
    ("ANONYMOUS"). Comparing by identity (`is TrustLevel.ANONYMOUS`) then fails
    for the string form and silently skips token promotion / the unauthenticated
    guard. Normalize by VALUE so both forms behave identically; anything
    unrecognized falls back to the secure default (ANONYMOUS).
    """
    if isinstance(value, TrustLevel):
        return value
    try:
        return TrustLevel(value)
    except (ValueError, KeyError, TypeError):
        return TrustLevel.ANONYMOUS


def _resolve_trust(request: Request) -> tuple[TrustLevel, bool]:
    """Resolve verified caller trust and whether entry-token auth promoted it."""
    trust = _coerce_trust(getattr(request.state, "trust_level", TrustLevel.ANONYMOUS))

    # S-3 entry-point exception: INVOKE_AUTH_TOKEN is read from os.environ only
    # here because it authenticates the caller before any InvocationContext exists.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    token_authenticated = False
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
        token_authenticated = True
    return trust, token_authenticated


def _require_verified_trust(trust: TrustLevel) -> None:
    """Reject callers without externally verified or internal trust."""
    if trust not in {TrustLevel.VERIFIED_EXTERNAL, TrustLevel.INTERNAL}:
        raise HTTPException(status_code=403, detail="Verified caller trust is required.")


def _valid_scope_value(value) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )


def _normalize_scope(candidate) -> dict[str, str] | None:
    """Accept only a mapping carrying every declared key with a clean value."""
    if not isinstance(candidate, Mapping):
        return None
    scope = {key: candidate.get(key) for key in SCOPE_KEYS}
    if any(not _valid_scope_value(value) for value in scope.values()):
        return None
    return scope


_UNSTAMPED = object()


def _scope_from_verified_state(request: Request) -> dict[str, object] | None:
    """Read verified scope attrs; None only when no attr was stamped at all.

    Explicitly stamped empty/None values are NOT "absent" — they mean a broken
    verifier and are returned as-is so the caller rejects them with 403
    instead of silently falling back to the STG default. Only a request whose
    state carries none of the declared attrs may use the fallback.
    """
    values = {key: getattr(request.state, key, _UNSTAMPED) for key in SCOPE_KEYS}
    if all(value is _UNSTAMPED for value in values.values()):
        return None
    return {key: (None if value is _UNSTAMPED else value) for key, value in values.items()}


def _scope_from_stg_default() -> dict[str, str] | None:
    # No cleanup of env values: a padded/multiline STG env var is a deployment
    # typo that must surface as 403, not be silently repaired.
    return _normalize_scope({key: os.environ.get(STG_DEFAULT_SCOPE_ENVS[key], "") for key in SCOPE_KEYS})


def _require_scope(request: Request, *, token_authenticated: bool) -> dict[str, str]:
    """Return verified scope, allowing STG defaults only after token auth.

    The STG fallback fires ONLY when no verified scope is present at all
    (seam returned None). A present-but-invalid scope (missing key, empty,
    padded, non-string) is a 403 — it must never be rescued by the STG
    default, or a broken verifier would silently serve the placeholder scope.
    """
    verify_scope = getattr(request.app.state, "verify_scope", _scope_from_verified_state)
    candidate = verify_scope(request)
    if candidate is None:
        scope = _scope_from_stg_default() if token_authenticated else None
        if scope is None:
            raise HTTPException(status_code=403, detail="Authenticated caller scope is required.")
        return scope
    if not isinstance(candidate, Mapping):
        # A stale pre-v0.3.0 seam returning a tuple is a wiring bug, not a
        # caller problem — fail loudly instead of a misleading 403.
        raise RuntimeError(
            "entry_adapter: verify_scope must return a Mapping keyed by SCOPE_KEYS or None (v0.3.0 contract)"
        )
    scope = _normalize_scope(candidate)
    if scope is None:
        raise HTTPException(status_code=403, detail="Authenticated caller scope is required.")
    return scope
