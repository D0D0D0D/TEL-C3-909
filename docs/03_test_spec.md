# Test Specification — TEL-C3-909 MonitoringAlertFirstTriageAgent

## Test Strategy
- Coverage target: every custom node (initialize/act/finalize) covered on success + error/edge paths; full graph covered by integration tests; all 6 diagnostic tools covered; runbook DSL validation pipeline covered exhaustively.
- Test types: Unit (`tests/unit/`) / Integration (`tests/integration/`) / Proof-of-Boundary (`tests/proof_of_boundary/`)
- LLM/webhook/gateway transport are fake/mocked at the injection seam per `sdk/how-to/write-tests.md` — no real network calls in CI.
- **PB-7 (HITL interrupt propagation) is N/A for this template** — `hitl.enabled: false` (docs/02_design.md §Design Decision Record: read-only actions have no approval-worthy operation; `target_allowlist` changes are approved via Git MR governance instead, not runtime HITL).

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: `src/schemas/state.py` re-exports `AutonomousState` (no redeclaration) | `tests/proof_of_boundary/test_state_safety.py` static scan: no credential-like field names, no Pydantic/dataclass annotations | PASS |
| TC-02 | SecurityViolationError / domain rejection fires on invalid input | `test_triage_initialize_node.py::TestSecurityGateInput` — empty/invalid-JSON/non-object/oversized alert payload all set `status=error` | PASS (6 tests) |
| TC-03 | No JWT/Credential in `src/` | CI `gate-credential-scan` — 0 violations; all test credentials use `mock-readonly-cred-for-testing` literal | PASS (local grep confirms no real secrets in `src/`) |
| TC-04 | InvocationContext via `configurable`/`ctx.secrets` only | All 6 tools resolve `DIAG_READONLY_CREDENTIALS` via `current_secrets().require()` / `ctx.secrets`; `TriageActNode` resolves `CHATOPS_WEBHOOK_URL` the same way; no `os.environ` calls anywhere in `src/` | PASS (grep: 0 `os.environ` occurrences in `src/`) |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start`/`node_complete`/`node_error` never emitted manually inside any custom node's `execute()`/`on_initialize()`/`on_finalize()` — only domain events (`alert_received`, `runbook_fetched`, `runbook_converted`, `runbook_matched`, `diagnostic_executed`, `report_pushed`, `triage_assembled`) | PASS (code review + `test_pb_invoke_order.py` confirms exact framework-owned event sequence) |
| TC-06 | S-2 `_security_gate_input()` non-bypassable | `FunctionNode.__init_subclass__` raises `TypeError` if a subclass defines `_security_gate_input` directly — verified structurally (none of our nodes define it; only `_extra_security_gate_input`) | PASS (import succeeds; attempting to define it would fail at class-definition time) |
| TC-07 | S-3 `_security_gate_output()` non-bypassable | Same as TC-06 for `_security_gate_output` / `_extra_security_gate_output` | PASS |
| TC-08 | `required_trust_level` declared and enforced (S-1 caller contract, docs/02 §Security Model S-1) | `test_triage_act_node.py::TestTriageActNodeTrustGate` (VERIFIED_EXTERNAL denied via `__call__`, INTERNAL allowed) + `test_tc08_trust_level_caller_contract.py` (full-graph: INTERNAL caller gets full diagnostic execution; VERIFIED_EXTERNAL caller gets a valid non-null degraded report, not a crash) | PASS (4 tests) |
| TC-09 | S-2 `_extra_security_gate_input()` non-trivial | `TriageInitializeNode`: JSON schema/size validation, prompt-injection separation (flags but does not reject instruction-like alert text), oversized-payload rejection | PASS (6 tests in `test_triage_initialize_node.py::TestSecurityGateInput`) |
| TC-10 | S-3 `_extra_security_gate_output()` non-trivial | `TriageFinalizeNode`: assertive-phrasing block (NFKC+casefold normalized), evidence-less hypothesis demotion, credential/PII residue check, escalation-required preservation check | PASS (7 tests in `test_triage_finalize_node.py::TestExtraSecurityGateOutput` + `TestAssertivePhrasingDetection`) |
| TC-11 | S-4: ≥1 domain `emit_trace_event()` per node's `execute()` path | `TriageInitializeNode` emits up to 4 domain events per invocation; `TriageActNode` emits `diagnostic_executed`/`report_pushed` per tool call; `TriageFinalizeNode` emits `triage_assembled` | PASS (verified via debug run — see integration test output; not independently unit-asserted via monkeypatch beyond PB-6, which already exercises the `emit_trace_event` call path per node) |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Result |
|-------|----------|------|----------------|--------|
| PB-1 | BaseNode → AuditLogger | `test_pb_invoke_order.py` (framework `emit_trace_event` fires node_start/node_complete for every custom node) | No silent failures | PASS |
| PB-2 | State serialization | `test_state_safety.py` (static) + `test_pb5_checkpoint_credential_safety.py` (dynamic full-invoke scan) | No Pydantic/dataclass; no credential leakage into invoke() result | PASS |
| PB-3 | L1 → External service | `test_pb3_external_service.py` — tool call reaches the actual `httpx.get`/`socket.create_connection` call boundary (transport itself mocked, matching CI's no-network policy; every layer above the transport call is real) | Real connection attempt at the transport boundary, correct URL/host:port constructed | PASS |
| PB-4 | Import isolation | `test_import_isolation.py` (AST scan of `src/`) | 0 violations (no `agenticstar` import) | PASS |
| PB-5 | Checkpoint safety | `test_pb5_checkpoint_credential_safety.py` — full `invoke()` with a distinctive mock credential marker; asserts the marker never appears anywhere in the result dict (the same shape LangGraph would checkpoint) | No credential in checkpoint-equivalent output | PASS |
| PB-6 | Invoke execution order | `test_pb_invoke_order.py` — S-1 → node_start → S-2 → execute() → S-3 → node_complete, for every concrete node under `src/nodes/` (custom node constructor args supplied via `_node_factory_args`) | Order verified for `TriageInitializeNode`, `TriageActNode`, `TriageFinalizeNode` | PASS |
| PB-7 | HITL interrupt propagation | **N/A** — `hitl.enabled: false` (see Test Strategy above) | Skipped, documented | N/A |

## Domain-Specific Tests (docs/02 write-not-executable requirement)

| Test file | Coverage |
|---|---|
| `test_readonly_gateway.py` | Allowlist enforcement (GET/HEAD/TCP), no write-HTTP-method code path, success/ng/failed status mapping |
| `test_pb_write_not_executable.py` | Physical proof: `ReadonlyGateway` has exactly `{http_get, http_head, tcp_connect, is_allowed}` as public methods; unregistered destination rejected; unregistered tool name rejected by `TriageActNode` |
| `test_runbook_service.py` | DSL schema validation, unregistered-tool-reference rejection, cycle detection, unreachable-node detection, instruction-like-text detection, credential scrub, conversion cache (LRU + hit/miss), valid-DSL fast path (no LLM call), LLM-conversion failure handling |
| `test_alert_adapters.py` | Alertmanager/Datadog/PagerDuty/generic payload normalization + adapter auto-detection restricted to `input_adapters` config |
| `test_diagnostic_tools.py` | All 6 tools present with exact names; `record_hypothesis` has no external reach and needs no secrets; all other tools route through the gateway and are msgpack-safe |
| `test_triage_initialize_node.py` | S-2 domain hook; all 4 `degraded_mode` trigger paths (no location / fetch failure / insufficient trust / no DSL match) each set a distinct `degraded_reason` |
| `test_triage_act_node.py` | Tool execution, unknown-tool rejection, exception handling, `record_hypothesis` special-casing, incremental ChatOps push (success / retry-then-undelivered / missing-secret), S-1 gate via `__call__` |
| `test_triage_finalize_node.py` | `assemble_output()` invocation from `on_finalize()`, prior-status preservation (error/cancelled not forced to success), S-3 domain gate (assertive block, evidence-less demotion, credential residue, escalation preservation) |
| `test_graph_abstract_surface.py` | `max_iterations`/`get_tools`/`build_think_prompt`/`assemble_output` behavior across degraded/normal/cancelled/error states |
| `test_graph_register_nodes.py` | CASE 2 composition: initialize/act/finalize replaced, think left as framework default, `add_edges()`/`_route_after_think()`/`route()` not overridden |
| `test_graph.py` (integration) | Full compile+invoke: degraded (no runbook), happy path (valid DSL + diagnostics + hypothesis), no-match degraded, budget-exhaustion partial report, empty-input rejection |
| `test_tc08_trust_level_caller_contract.py` | S-1 caller contract end-to-end: INTERNAL full execution vs. VERIFIED_EXTERNAL degraded report |

## Known Framework-Level Limitation Discovered During Implementation

`FinalizeNode.execute()` (and therefore `TriageFinalizeNode.on_finalize()` / `assemble_output()`) is **unconditionally skipped by `BaseNode.__call__()`** whenever the incoming `state["status"]` is already the literal string `"error"` — verified directly against `framework/nodes/function_node.py` + `framework/nodes/base_node.py` (not a design assumption; reproduced with a minimal `FunctionNode` subclass outside this template's code). This means any error status that reaches `finalize` unresolved (e.g. an uncaught tool exception, or `TriageActNode`'s own S-1 denial if ever reached) will short-circuit `finalize` and return `output: None` instead of the "always produce a partial report" guarantee docs/02 specifies.

**Mitigation implemented**: `TriageInitializeNode.on_initialize()` detects an insufficient caller trust level (`caller_trust_level != INTERNAL`) *before* the loop starts and forces `degraded_mode=True` / `degraded_reason="insufficient_trust_for_diagnostics"`, so a prompt-compliant LLM never emits `tool_calls` and `TriageActNode`'s S-1 gate is never reached in the realistic case. This is verified by `test_tc08_trust_level_caller_contract.py` and `test_triage_initialize_node.py::test_insufficient_trust_enters_degraded_mode_and_never_fetches`.

**Residual gap (not fully closable from template code)**: this mitigation relies on the LLM respecting the hard "never emit tool_calls" instruction in `build_think_prompt()`. It is not a structural/physical guarantee (unlike the read-only 5-layer enforcement) because `__call__()` must never be overridden (S-1 gate integrity) and `think` must remain the framework default per docs/02's CASE 2 contract. If an LLM hallucinates a tool call anyway, the resulting S-1 denial in `act` will still cause `finalize` to be skipped and `output=None`. This is flagged as an open item — see docs/02 Open Items.

## Test Execution Summary
- Execution date: 2026-07-06
- Total tests: 135 (`pytest tests/ -v` reports 135 passed, which includes the proof-of-boundary subset)
- Pass: 135 / Fail: 0 / Skip: 0 (PB-7 documented N/A, not a pytest skip)
- `pytest tests/proof_of_boundary/ -v`: 13 passed
- Coverage: all custom nodes, all 6 tools, DSL validation pipeline, and the full graph loop (happy path / degraded / cancelled / trust-denied / empty-input) are exercised.

## Pre-push adversarial review — 3 real bugs found and fixed

An adversarial review of the actual source files (not a summary) before push found 3 genuine
implementation bugs, none of which were the known framework limitation above:

1. **Layer-① credential never used on the wire** (High) — all 4 HTTP-based diagnostic tools resolved
   `DIAG_READONLY_CREDENTIALS` via `_resolve_readonly_credential()` but discarded the value instead of
   presenting it to the target endpoint. Fixed: `ReadonlyGateway.http_get/http_head` now accept an
   optional `credential` parameter sent as a `Bearer` Authorization header; the 4 tools (`http_get_healthcheck`,
   `read_metric`, `tail_log`, `check_liveness`) now pass it through. Covered by
   `test_pb3_external_service.py::test_http_get_healthcheck_sends_resolved_credential_as_bearer_header`.
2. **Hardcoded `http://` scheme breaks TLS-only endpoints** (High) — `ReadonlyGateway` always built
   `http://{target}{path}` even for conventionally-TLS ports (443/8443), which both fails the connection to
   HTTPS-only endpoints and would send the Authorization header in clear text if it somehow worked. Fixed:
   `_scheme_for_port()` maps 443/8443 → `https`, everything else → `http`. Covered by
   `test_pb3_external_service.py::test_http_get_healthcheck_reaches_httpx_transport` (443→https) and
   `test_http_get_healthcheck_non_tls_port_uses_http` (8080→http).
3. **`unchecked` always reported the full decision tree as unchecked** (Medium) — `TriageActNode` synthesized
   `visited[].node_id` as `f"{tool_name}:{target}:{index}"`, which never matched the DSL's actual node `id`
   values (e.g. `"check-health-ep"`), so `assemble_output()`'s `unchecked` computation always returned every
   DSL node regardless of what actually ran — defeating the "ここまでは白" transparency feature entirely.
   Fixed: tools now accept an explicit `dsl_node_id` parameter; `build_think_prompt()` instructs the LLM to
   echo the DSL node's own `id` field back via this parameter; `TriageActNode` prefers it for `visited[].node_id`
   (falling back to the synthetic id only when absent — the safe/conservative failure mode). Covered by
   `tests/integration/test_graph.py::test_valid_dsl_full_diagnostic_loop` (asserts `unchecked == ["check-db"]`
   after checking `"check-health-ep"`, and `checked_matrix[0]["node_id"] == "check-health-ep"`).
4. **`runbook_conversion.enabled: false` was read from config but never consulted** (High) — a deployment
   that disabled runtime LLM conversion (and therefore had no conversion LLM configured) would crash with an
   uncaught `AttributeError` on `llm.complete()` instead of gracefully raising `RunbookValidationError` (→
   degraded mode) when the fetched runbook wasn't already valid DSL. Fixed: `load_decision_tree()` now takes
   `conversion_enabled` explicitly and raises `RunbookValidationError` instead of calling the LLM when disabled.
   Covered by `test_runbook_service.py::test_conversion_disabled_unstructured_text_raises_not_crashes` and
   `test_conversion_disabled_valid_dsl_still_works_without_llm`.

All 4 fixes are reflected in the test counts above (135/13, up from the pre-fix 131/11).

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | Azure OpenAI 上の GPT モデルを `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['summary', 'recommended_actions', 'incident_report_draft', 'escalation_required'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（外部 LLM の鍵が要るため）。
