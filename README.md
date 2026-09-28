# TEL-C3-909 — Monitoring Alert First-Triage Agent

> **Category**: Cat 3 (autonomous think → act → observe loop)
> **Industry**: TEL

## Overview

This agent takes a monitoring alert and performs the first few minutes of triage — the part where
an operator works out *what is actually wrong* — using read-only checks only. It receives the
webhook payload, follows the operations runbook to decide what to look at next, runs health
checks, connectivity probes, metric reads and log tails against approved targets, and returns
what it checked, its ranked cause hypotheses with confidence and evidence, and what it recommends
a person do next.

It suits the moment an alert fires out of hours: an on-call engineer who wants to know how far
triage has already got and what the leading hypothesis is before opening a laptop, an operations
team that wants first-line triage to follow the runbook rather than the individual, and a
response channel that wants progress reported as it happens rather than at the end.

This is a **Cat 3** template, and the distinction matters here. The agent decides its own next
check from what it has observed, and decides when it has learned enough to stop — the strategy
lives in the prompt the agent builds each turn, not in a fixed pipeline. A fixed pipeline cannot
express "look at this next *because* of what the last check returned".

Read-only is enforced structurally, not by instruction:

- The diagnostic tools are six read-only operations — health check, connectivity probe, metric
  read, log tail, liveness check and hypothesis recording. There is no write tool, so a model
  that decided to restart something has nothing to call.
- The HTTP layer implements GET and HEAD only. There is no POST/PUT/DELETE/PATCH code path to
  reach.
- Diagnostic targets come from a pre-approved allow-list, and the agent cannot extend it —
  changing it is a reviewed change to the repository, not a runtime decision.
- Credentials issued to the agent are read-only ones. A target that cannot be given a read-only
  credential must not be added to the allow-list.

The agent never states a cause as fact: every finding is hypothesis + confidence + evidence, and
the decision to act — restart, roll back, change configuration — is always a person's.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- a webhook path from your monitoring system. The standard caller is a trusted monitoring
  receiver invoking at internal trust; a verified-external caller is a defined, tested degraded
  path;
- your operations runbook, at `runbook_source.location`. Unstructured runbooks are converted at
  runtime into the decision tree the agent follows, cached by document hash so a changed document
  re-converts automatically. **Without a reachable runbook the agent does not diagnose** — it
  degrades to receive-and-identify only;
- an allow-list of diagnosable targets. Empty means degraded mode: no diagnostics are performed;
- the secrets named in the manifest: `DIAG_READONLY_CREDENTIALS` (read-only access to diagnostic
  targets), `CHATOPS_WEBHOOK_URL` (incremental reporting destination) and `ANTHROPIC_API_KEY`.

Runtime parameters read from `config/config.yaml` (defaults in parentheses) — the first three are
starting values to tune against your own decision-tree depth, alert volume and cost policy:

| Key | Meaning |
|---|---|
| `budget_usd` (`1.50`) | Hard model-cost ceiling per invocation, runbook conversion included |
| `max_iterations` (`24`) | Ceiling on think ⇄ act cycles |
| `confidence_threshold` (`0.75`) | The convergence signal: at this confidence the agent stops on its own |
| `target_allowlist` (`[]`) | Diagnosable destinations as `host:port`; empty = degraded mode |
| `runbook_source.mode` (`cached`) / `.location` (`""`) | Runbook retrieval; `cached` keys the converted tree by document hash, `per_invocation` always re-fetches |
| `runbook_conversion.enabled` (`true`) | Runtime conversion of unstructured runbooks; `false` accepts only pre-validated decision trees |
| `reporting.enabled` (`true`) / `.min_interval_seconds` (`0`) | Incremental progress reporting; `0` reports every observation |
| `output.language` (`ja`) | Report language |
| `input_adapters` | Accepted alert payload shapes |

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent runs inside the platform runtime and takes its secrets from the platform secret provider. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR, and the agent does not fall back to a degraded
mode when it is absent. Without the framework package installed, `src/api/server.py` cannot be
imported at all — it imports `framework.*` at module level, so start-up ends in `ImportError`.
With the framework present, the model client is built **eagerly at start-up**, so the first
`/invoke` is never the first time the wiring is exercised — but the construction is guarded, so a
broken environment reports unready rather than making the module unimportable. Exactly one
condition degrades: a *typed absence* of the model key falls back to a deterministic offline
stand-in, so `/invoke` runs without external credentials. Any other failure — a provider timeout,
an authorisation failure, a secret-store outage — propagates and fails the readiness probe,
because a secret-store outage that looked like "no key configured" would come up healthy and
answer with empty triage at `status: success`. The test suite runs without a platform connection.

## Input

`POST /invoke` takes a JSON body with `input` and `session_id`. `input` is a **JSON string**
holding the alert payload; Alertmanager, Datadog, PagerDuty and a generic shape are accepted.

Callers must be authenticated: trust below verified is refused before the graph runs. `GET
/livez` is liveness and always answers 200 — restarting will not conjure a missing secret, so it
must not flap the deployment. `GET /health` is readiness and returns 503 while the agent is
unusable.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "session_id": "demo-001",
    "input": "{\"groupKey\":\"x\",\"alerts\":[{\"labels\":{\"alertname\":\"HighLatency\",\"service\":\"web-portal\",\"severity\":\"critical\"},\"annotations\":{\"summary\":\"p99 latency high\"},\"startsAt\":\"2026-07-06T00:00:00Z\"}]}"
  }'
```

## Output

The standard AgentCore envelope is preserved. `output` carries the triage report:

```json
{
  "summary": {
    "status": "一次切り分け 完了 / 確認 1/2",
    "top_hypothesis": "possible network issue（確度 0.80）",
    "next_action": "最有力仮説に基づく対処の実行判断は人間（NOC/SRE）が行ってください。 (escalation_required: false)",
    "escalation_required": false
  },
  "checked_matrix": ["..."],
  "hypotheses": [{"cause": "possible network issue", "confidence": 0.8, "evidence": ["..."]}],
  "recommended_actions": ["..."],
  "runbook_trace": {"doc_hash": "...", "cache_hit": false}
}
```

`checked_matrix` is what was actually looked at, so an incomplete triage is visible as incomplete
rather than reading as an all-clear. `runbook_trace` records which runbook version drove the
decisions. A run that ended in error or was cancelled keeps that status — it is not overwritten
with success. Values above are illustrative.

## Security and Limitations

Read-only is enforced by the tool set and the transport, not by prompt instruction, and the
diagnosable surface is bounded by an allow-list the agent cannot change. Root-cause phrasing in
the final report is checked for assertive wording after Unicode normalisation and case folding,
so full-width and mixed-case variants cannot slip a definitive claim past the check.

Known limits:

- **The report is hypotheses, not a diagnosis**, and every remediation decision is a person's.
- **No runbook means no diagnosis.** With the runbook unreachable or absent, the agent degrades
  to receiving the alert and identifying the target.
- **An empty allow-list means no diagnostics**, by design.
- **One instruction is not structurally enforced.** The agent is told never to emit tool calls in
  one specific situation, and that particular constraint rests on the model honouring it rather
  than on a physical guard, because the surrounding framework contracts cannot be overridden to
  enforce it. If a model ignores it, the trust gate still denies the call and the run ends
  without a report rather than doing something unsafe. This is recorded as an open item.
- **Cost and depth are capped** by `budget_usd` and `max_iterations`; a triage that would exceed
  them stops and escalates rather than continuing.

Output is machine-generated and assistive. It does not substitute for expert review, and no
remediation should be performed on its basis without human verification.

## Release

- Version: 1.0.0
- Category: autonomous think → act → observe loop (Cat 3)
- Generation mode: `llm`
- HITL: not enabled — every action is read-only, so there is no approval-worthy operation at
  runtime; allow-list changes are approved by repository review instead
- Persistence: none — no checkpointer; cross-invocation correlation is a future option
- Secrets: `DIAG_READONLY_CREDENTIALS`, `CHATOPS_WEBHOOK_URL`, `ANTHROPIC_API_KEY`

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using the offline stand-in for the model and fakes for
the diagnostic targets.

## Documentation

`docs/02_design.md` carries the autonomous loop wiring, the runbook decision-tree format, the
security model and the final output shape. `docs/03_test_spec.md` records the test specification
and the verification results, including a live-model run.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
