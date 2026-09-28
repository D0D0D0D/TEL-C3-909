"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.
#
# TEL-C3-909: the standard caller is the monitoring webhook receiver, which
# invokes with caller_trust_level=INTERNAL (docs/02_design.md §Security
# Model S-1 "呼び出し元契約"). A VERIFIED_EXTERNAL degraded-mode call path is
# also a defined, tested behavior (TC-08 family).
#
# The LLM is injected via the seam in src/services/llm_client.py: a real
# shared.services.llm client in production, or a deterministic offline
# stand-in when none can be constructed (so /invoke runs without external
# credentials). Construction is eager so the first /invoke is never the first
# time the wiring is exercised (Lodging Guest Multilingual Concierge Agent lesson), but guarded so a broken
# environment degrades to 503 instead of making the module unimportable — CI
# never executes start-up, so an import-time failure hides behind green gates.

import logging
import pathlib
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory

from src.entry_adapter.auth import _require_verified_trust, _resolve_trust
from src.graph.graph import Graph
from src.services.llm_client import build_llm

_logger = logging.getLogger(__name__)

app = FastAPI(title="MonitoringAlertFirstTriageAgent")

_secrets_provider = secrets_factory(namespace="tel-c3-909", agent_name="tel_c3_909")


_CONFIG_YAML = pathlib.Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _load_agent_config() -> dict:
    """Runtime parameters live in config/config.yaml; agent.yaml stays static.

    The operator-tunable settings include `runbook_source.location`
    (the customer's runbook) and `target_allowlist` (the destinations that may
    be diagnosed). Hardcoding them here — as this adapter previously did — meant
    that following the deployment procedure in docs/07 changed nothing: the
    process always came up with an empty location and an empty allowlist, so
    every run degraded to `runbook_location_not_configured` and no diagnostic
    was ever executed. The STG smoke check could not surface it either, because
    that degraded path still returns status=success.
    """
    with open(_CONFIG_YAML, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _build_agent() -> Graph:
    # Runtime config first, then the injected seam. `llm` is a live client, so
    # it is layered on top rather than read from YAML.
    config = _load_agent_config()
    config["llm"] = build_llm(_secrets_provider)
    config.setdefault("memory_enabled", False)
    config.setdefault("hitl", {"enabled": False})

    graph = Graph(config=config)
    graph.compile()
    graph.provision_secrets(_secrets_provider)
    return graph


try:
    agent: Graph | None = _build_agent()
    _agent_init_error: Exception | None = None
except Exception as exc:  # noqa: BLE001 - reported via /health 503 + /invoke 503
    agent, _agent_init_error = None, exc
    _logger.exception("agent initialisation failed; serving 503 until resolved")


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    """Run one triage.

    Caller authentication uses the entry-adapter part's auth core only —
    `authenticate_scope()` is deliberately NOT wired. Scope keys are KB
    partition keys, and this agent owns no KB: the decision tree comes from
    the customer's runbook and diagnostics are bounded by `target_allowlist`,
    neither of which is a per-request partition. Gating on a scope that
    nothing consumes would be STG-default theatre (parts/entry-adapter
    USAGE.md requires this rationale to be recorded here).
    """
    if agent is None:
        raise HTTPException(status_code=503, detail="agent unavailable")

    trust, _token_authenticated = _resolve_trust(request)
    _require_verified_trust(trust)

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", "") or "",
        )
        return agent.invoke(req.input, ctx=ctx)


@app.get("/livez")
def livez():
    """Liveness: the process is up. Always 200 — restarting will not conjure a
    missing secret, so this must not flap the deployment."""
    return {"status": "alive", "agent": "tel_c3_909"}


@app.get("/health")
def health(response: Response):
    """Readiness. Returns 503 while the agent is unusable.

    A 200 "degraded" body would keep the load balancer sending traffic to a
    process whose every /invoke answers 503. The failure cause is logged, not
    returned, so a reachable probe cannot be used to enumerate which
    dependency or secret is missing.
    """
    if agent is None:
        response.status_code = 503
        return {"status": "unavailable", "agent": "tel_c3_909"}
    return {"status": "ok", "agent": "tel_c3_909"}
