"""LLM injection seam for TEL-C3-909.

`AutonomousBaseGraph` requires a `llm` in config with a `.complete()` /
`.bind_tools()` interface (`shared.services.llm.base_llm.BaseLLM`). In
production an Anthropic client from `shared.services.llm` is injected.

When no credential is provisioned — offline local runs, and the deploy-stg
smoke check — `OfflineTriageLLM` takes its place so `/invoke` still runs
without external credentials. This is the project-standard pattern for offline/STG runs
(Enterprise Deep Research & Long-Form Report Generation Agent `ScriptedResearchLLM`; the Cat 2 equivalent is Employee Financial Wellness Self-Advisory Agent /
AssetInspectionHistoryAdvisoryAgent, where a failed client construction leaves `llm=None` and the graph
falls back to a deterministic path).

Deliberately NOT gated on environment variables. An earlier revision selected
the stand-in from `CI` + `CI_JOB_NAME` + `STG_MOCK_MODE`; that is unsound in
both directions — `CI=true` is set by every GitLab job including production
deploys, and env vars are inherited — so a production process could be stubbed
out, while a CI job holding a real key would bill the API.

The credential, not a try/except around construction, is what selects the
path. Once a key IS provisioned this module never falls back: a construction
failure there is a real fault and propagates, so a production process with a
broken client fails its readiness probe instead of quietly serving empty
triage results.
"""

from __future__ import annotations

from typing import Any, Iterator

from framework.secrets.base import MissingSecret
from shared.services.llm.base_llm import BaseLLM

_LLM_SECRET_KEY = "ANTHROPIC_API_KEY"

_OFFLINE_ANSWER = (
    "オフライン実行（LLM 未接続）のため、診断は実行していません。"
    "アラートの受信内容と対象の特定のみを報告します。"
)


class OfflineTriageLLM(BaseLLM):
    """Deterministic stand-in used when no LLM credential is provisioned.

    Emits no tool calls, so `ThinkNode` finalizes on its first iteration and
    `/invoke` returns a well-formed degraded report instead of failing. It is
    deliberately NOT a simulation of the triage loop: an earlier revision had
    it synthesize `record_hypothesis` calls to "exercise act", which both
    violated the tool contract (`record_hypothesis(cause, confidence,
    evidence)` — no node id) and could not converge, because
    `record_hypothesis` intentionally does not advance `visited`. Only a real
    diagnostic tool call does that, and choosing one offline would mean
    inventing diagnostics against a customer runbook this process has not
    loaded.

    The act phase is covered where it can be exercised honestly:
    `tests/integration/test_graph.py` scripts multi-turn tool calls against a
    real decision tree.

    Stateless across invocations, so one compiled agent can serve many
    requests (the node-contract rule against instance state).
    """

    def bind_tools(self, tools: list) -> "OfflineTriageLLM":
        return self

    def stream(self, messages: Any) -> Iterator[str]:  # pragma: no cover - unused offline
        yield _OFFLINE_ANSWER

    def complete(self, messages: Any) -> dict:
        return {"content": _OFFLINE_ANSWER, "tool_calls": []}


def build_llm(secrets_provider) -> BaseLLM:
    """Return the LLM client for this deployment.

    Selection is on the credential being ABSENT — specifically on
    :class:`MissingSecret`, the typed signal the provider raises for "this key
    is not configured here":

      MissingSecret        -> offline stand-in (a supported degradation)
      any other failure    -> PROPAGATES (provider timeout, authorization
                              failure, secret-store outage, ...)
      credential present   -> real client, and any failure building it also
                              propagates

    Catching bare `Exception` here would be wrong in a way that is hard to
    notice: a secret-store outage in production would look identical to "no
    key configured", so the process would come up healthy on the stand-in and
    answer /invoke with empty triage at status=success — and deploy-stg's
    agent_status_success would pass too. Only the typed absence degrades;
    everything else fails the readiness probe.
    """
    try:
        api_key = secrets_provider.require(_LLM_SECRET_KEY)
    except MissingSecret:
        return OfflineTriageLLM()

    from shared.services.llm.anthropic_client import AnthropicClient

    # The concrete model identity is an operational decision — AnthropicClient applies its
    # own default rather than this template hardcoding one.
    return AnthropicClient({"api_key": api_key})
