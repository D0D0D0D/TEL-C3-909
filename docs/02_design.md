# Template Design Specification — TEL-C3-909

MonitoringAlertFirstTriageAgent（監視アラート一次切り分け 自律アシスト / Cat 3）

## Position in AgentCore Architecture

- **Agent Class**: `MonitoringAlertFirstTriageAgent`
- **L1 Base**: AutonomousBaseGraph（**Cat 3 は AutonomousBaseGraph を直接継承** — 自律的な
  観測駆動の経路選択と終了判断に必要。AgentBaseGraph は固定パイプラインであり、
  これらを満たせないため不可）
- **Three-Layer Separation**:
  - State: `AutonomousState`（framework 提供の flat TypedDict）をそのまま利用。テンプレートでの再宣言禁止。
    独自 Pydantic / dataclass 不可（msgpack 非互換）。
  - Node: L1 継承（Template Method: `execute(self, state) -> dict` のみ override、変更キーのみ返却）
  - Graph: composition（`register_nodes()` で initialize / act / finalize スロットを差し替え — CASE 2）

> 先行 Cat 3 実証: Enterprise Deep Research & Long-Form Report Generation Agent（AutonomousBaseGraph・custom finalize 差し替えパターン）。
> SDK 契約は `agenticstar-agentcore==1.0.0`（released wheel）準拠。

## Architecture Overview

### Pipeline（Cat 3 固定 backbone — `add_edges()` は override 禁止）

```
START → initialize → think ⇄ act → finalize → END
```

ルーティング（`AutonomousBaseGraph._route_after_think(state)`、override 不可）:

```
think 実行後:
  iterations >= max_iterations        → finalize   （安全上限。config max_iterations=24）
  status in (success/error/cancelled) → finalize   （agent が「十分」と判断 = SUCCESS / budget 枯渇 = CANCELLED）
  tool_calls あり                      → act        （read-only 診断を実行 → think へ戻る）
  それ以外                             → finalize
```

一次切り分けの自律ループは「think が決定木の未確認枝・異常枝から次に実行する read-only 確認を
`tool_calls` として発行し、決定木を辿り切った / 確度しきい値に達した時点で `tool_calls` を出さず
最終切り分け結果（SUCCESS）にする」ことで実現する。終了判断は `build_think_prompt()` ＋ LLM ＝
**agent が所有**する（Cat 3 成立要件）。

### Node Configuration

| Node | 責務 | 入力 State | 出力 State | Inherits / Overrides |
|------|------|-----------|-----------|----------------------|
| initialize | ループ State 初期化＋**アラート JSON 正規化・Runbook 取得・（非構造手順書なら）決定木 DSL への実行時 LLM 変換・マッチング**。手順書テキストは credential scrub 後にのみ LLM / State へ。`cost_usd=0.0`、`caller_trust_level` 保全 | user_input（アラート webhook payload） | working_memory（alerts / runbook_ref / decision_tree / visited=[]）、iterations=0、cost_usd（変換 LLM コスト計上） | **`TriageInitializeNode(AutonomousInitializeNode)`（custom・差し替え）** |
| think | `build_think_prompt()` で「確認済み/未確認/異常枝・確度・残 budget」を提示し、次に実行する確認（tool_calls）or 収束（最終回答）を LLM が判断。コスト加算・budget サーキットブレーカ | working_memory, tool_results, thoughts | tool_calls, thoughts, status, cost_usd | `ThinkNode`（framework default。トリアージ戦略は prompt 側で所有） |
| act | `tool_calls`（read-only 診断のみ）を実行し `tool_results` へ記録。**実行のたびに逐次報告を ChatOps へ push**（incremental reporting）＋ visited 更新 | tool_calls | tool_results, working_memory.visited / report_log | **`TriageActNode(ToolActNode)`（custom・差し替え）** |
| finalize | **`assemble_output()` を呼び一次切り分けレポートを構築**＋ S-3 ドメイン出力ゲート＋ S-4 domain audit。**prior status（error/cancelled）を保全**（無条件 SUCCESS 禁止） | working_memory, tool_results, thoughts, status | final_output, response_metadata | **`TriageFinalizeNode(FinalizeNode)`（custom・差し替え）** |

> ⚠️ **設計上の重要判断（assemble_output — 既知問題）**: released wheel では抽象メソッド
> `assemble_output()` は framework のどこからも呼ばれない（default `FinalizeNode.on_finalize()` は
> `{}` を返すのみ）。CASE 1（default finalize）だと `final_output` が永遠に未設定になる。よって本
> テンプレートは **CASE 2** で `register_nodes()` を override し、finalize スロットを custom
> `TriageFinalizeNode` に差し替え、`on_finalize()` 内で `self._agent.assemble_output(state)` を呼ぶ。
> その際 **prior status が error / cancelled ならそれを保全**し、無条件で SUCCESS に上書きしない
> （Enterprise Deep Research & Long-Form Report Generation Agent で実機確定し、敵対的コードレビューの指摘を反映したパターン）。

`register_nodes()` の実装形（CASE 2 — 3 スロット差し替え）:

```python
def register_nodes(self) -> None:
    super().register_nodes()   # initialize/think/act/finalize の 4 スロットを wire
    self._nodes["initialize"] = TriageInitializeNode(self)
    self._nodes["act"]        = TriageActNode(self.get_tools(), ...)
    self._nodes["finalize"]   = TriageFinalizeNode(self)
    # think は default ThinkNode のまま（戦略は build_think_prompt() が所有）
    # add_edges() / _route_after_think() は override しない
```

実装する abstract surface: `max_iterations`（property）/ `get_tools()` / `build_think_prompt()` /
`assemble_output()` の 4 点＋上記 CASE 2 差し替え。

### Cat 3 自律挙動 → 設計へのマッピング（4 点）

| # | 自律挙動 | 設計上の実現箇所 |
|---|---------|----------------|
| ① | 異常検知した branch への深掘り選択 | `build_think_prompt()` が visited（確認済み結果）と decision_tree の未確認枝を提示し、LLM が「異常が出た枝の深掘り or 別枝」を選択して tool_calls を発行 |
| ② | check 失敗 / timeout 時の retry-vs-代替パス自己判断 | tool 失敗は `tool_results` に `{status:"failed", reason}` として観測される。think prompt に「失敗時は (a) 1 回だけ再試行 (b) 別系統の確認へジャンプ (c) その症状自体を根拠に仮説を更新、のいずれかを理由付きで選べ」と行動原則を記述し、LLM が観測の意味理解で判断 |
| ③ | 相関アラート間の探索履歴共有 | Alertmanager 等の **group 通知（1 webhook payload に相関アラート配列）を 1 invocation で受ける**のを基本形とし、`working_memory.visited` を全アラート横断で共有 → 同一 (target, check_id) の重複チェックを think がスキップ。invocation を跨ぐ共有は `memory_enabled: true` ＋ `correlation_key` を thread_id に用いる構成をオプションとして定義（v0 は同一 payload 内共有まで） |
| ④ | budget / confidence による agent 自身の停止判断 | 3 層: (1) `budget_usd` 到達 → framework が `status=cancelled` で停止（hard ceiling）、(2) `max_iterations` 到達 → finalize（安全上限）、(3) **確度収束** — 確度を機械可読にするため、think は仮説を更新するたびに **`record_hypothesis` tool**（外部副作用なし・working_memory 書き込み専用）を呼び、`working_memory.hypotheses` に `{cause, confidence, evidence}` を蓄積する。`build_think_prompt()` は毎 iteration「現在の最大 confidence」と `confidence_threshold`（config、既定 0.75）を提示し、「決定木を辿り切った、または threshold 以上の仮説に到達したら tool_calls を出さず最終回答せよ」という停止規則を行動原則として与える（default `ThinkNode` のまま実現可能・終了判断は agent 所有）。finalize は `working_memory.hypotheses` からレポートを組成する。(1)(2) の打ち切り時も finalize は必ず部分レポートを出す（下記 usability 設計） |

### Data Flow

```
[監視システム] ──アラート発報──▶ webhook push（構造化 JSON・AG は監視系をポーリングしない）
     │  Alertmanager / Datadog / PagerDuty 等の payload（相関アラートは group で 1 payload）
     ▼
initialize ── payload を normalize（代表 3 形式は adapter が自動吸収、他は generic mapping）
     │        Runbook を取得（per_invocation）→ credential scrub → valid DSL ならそのまま /
     │        非構造テキストなら LLM が決定木 DSL へ実行時変換（キャッシュ hit ならスキップ）
     │        → DSL 検証（スキーマ / tool 参照 / 循環 / instruction-like text）→ 辿る木をマッチング
     │        S-4: alert_received / runbook_fetched / runbook_converted / runbook_matched を emit
     ▼
think ◀────────────────┐  build_think_prompt(): 確認済み/未確認/異常枝・仮説と確度・残 budget を提示
     │                 │                        「次の確認」or「収束」を LLM が判断（①②③④）
     │ tool_calls?     │
     ├─ yes ─▶ act ────┘  read-only allowlist の診断を実行（http_get_healthcheck / probe_connectivity /
     │          │          read_metric / tail_log / check_liveness）→ tool_results・visited 更新
     │          │          ★実行のたびに逐次報告 1 行を ChatOps へ push（緊急 cadence・溜めない）
     │ no（SUCCESS）or iterations>=max or budget 枯渇（CANCELLED）
     ▼
finalize ── assemble_output(): 3 行サマリ＋確認マトリクス＋原因仮説（確度・根拠付き）＋
     │       推奨対処（実行は人間）＋インシデント報告ドラフトを組成
     │       S-3: 断定表現ブロック・根拠なし仮説を「要確認」へ降格・credential/PII scrub 検証
     ▼
END → NOC/監視オペレータ・SRE（対処判断・エスカレーション）
      ※allowlist 外の操作が必要と判断した時点で「人間対応が必要」を明示して停止
```

### AGENTIC STAR Marketplace entry adapter

リポジトリ直下の `cli.py` は Marketplace 専用境界として既存
`MonitoringAlertFirstTriageAgent` を継承する `MarketplaceGraph` を公開する。runner が渡す
`input_context`（現在は `conversation_history`、将来キーも含む）は信頼済み scope ではないため、
既存値との merge は行わず破棄する。代わりに entry-adapter part が宣言する `SCOPE_KEYS` と
`STG_DEFAULT_SCOPE_ENVS` を唯一の対応表として、次の context を毎 invocation で組み直す。

```python
{
    "production_company_id": os.environ["STG_DEFAULT_PRODUCTION_COMPANY_ID"],
    "project_id": os.environ["STG_DEFAULT_PROJECT_ID"],
}
```

scope 宣言の不整合、値の欠落、空文字、値中の空白文字は fail closed とする。runner 契約どおり
`compile()` 後に呼ばれる `provision_secrets()` で Azure 接続値と scope を全件検証するため、
不正な Pod はリクエスト受付前に起動失敗する。`invoke()` でも scope を再解決・再検証し、起動後の
環境差替えを古い値で処理しない。allowlist provider は Azure 3 値と scope 2 値以外を公開しない。

Cat 3 の compile 契約を満たすため、adapter は credential を保持しない `MarketplaceAzureLLM`
（`BaseLLM`）を構築時に `config["llm"]` へ注入し、`budget_usd` が構築時 config にない場合は拒否する。
Azure の接続値と `AzureOpenAIClient` は `complete()` / `stream()` の呼出しごとに解決・生成し、
adapter instance には cache しない。AgentCore 1.0.0/1.0.1 にない config loader、Marketplace
bootstrap、environment provider、Azure client には同じ契約の compatibility path を持つ。

Marketplace の `InvocationContext` は常に `VERIFIED_EXTERNAL`、`hitl_allowed=False` に再構築する。
したがって S-1 が `INTERNAL` を要求する `act` は到達不能で、Marketplace が返せるのはアラートの
受信・対象特定・「診断には INTERNAL webhook 経路が必要」という縮退レポートまでである。この AG
には入力から選択できる ingest/update 等の書込 mode はなく、登録 tool と gateway にも書込経路は
ない。万一 `AWAITING_HUMAN` が返れば adapter は成功応答にせず失敗させる。

`budget_usd=1.50` と `max_iterations=24` は既存 Cat 3 設計値を保持し、Marketplace 都合で下げない。
`VERIFIED_EXTERNAL` の initialize は decision tree をロードせず縮退し、通常は Azure think 1 回で
finalize するため、24 iteration の最悪ループは Marketplace 経路では発生しない。よって通常の
Marketplace 実行時間には収まる見込みだが、platform の数値上限と Azure 1.0.2 client の timeout は
このリポジトリの契約外であり、外部 API 遅延まで含む絶対保証ではない。

### State Definition

State は framework の **`AutonomousState`**（`AgentState` 派生 flat TypedDict）をそのまま使う。
ドメインデータは独自 state.py を新設せず `working_memory`（dict）・`tool_results` に格納する。
`src/schemas/state.py` は `AutonomousState` を re-export し、`working_memory` のキー規約を
docstring で定義する（再宣言禁止）。

| Field | Type | Purpose | Source |
|-------|------|---------|--------|
| `user_input` | str | アラート webhook payload（JSON 文字列） | AgentState |
| `iterations` | int | think-act ループ回数（安全上限 = max_iterations） | AutonomousState |
| `thoughts` | list[str]（reducer add） | 各ループの LLM 判断ログ（辿った経路の説明責任） | AutonomousState |
| `tool_calls` | list[dict] | think が発行した次の確認 `{name, args}` | AutonomousState |
| `tool_results` | list[dict]（reducer add） | 診断結果 `{tool, result, args, status}` | AutonomousState |
| `working_memory` | dict | トリアージ作業領域（下記キー規約） | AutonomousState |
| `final_output` | Any | 一次切り分けレポート（dict） | AutonomousState |
| `cost_usd` | float | 累積 LLM コスト（budget サーキットブレーカ） | AutonomousState |

`working_memory` キー規約（トリアージ作業領域・すべて msgpack-safe）:

| key | type | purpose |
|-----|------|---------|
| `alerts` | list[dict] | 正規化済みアラート（相関 group 全件）`{alert_id, target_system, alert_type, severity, ts, metrics}` |
| `runbook_ref` | dict | マッチした Runbook の参照 `{runbook_id, version, source_mode, doc_hash, fetched_at, converted(bool), cache_hit(bool), redacted_credential_count}` — 変換の透明性（最終レポートに同梱）の基盤 |
| `decision_tree` | dict | 決定木 DSL（ノード=確認手順・ガード式・次ノード・tool 参照） |
| `visited` | list[dict] | 確認済みノード `{node_id, target, tool, result, verdict(ok/ng/failed), alert_ids}` — 相関アラート横断で共有（③） |
| `hypotheses` | list[dict] | 原因仮説 `{cause, confidence, evidence:[node_id]}` — think が `record_hypothesis` tool 経由で更新（機械可読な確度収束④の基盤） |
| `escalation_required` | bool | allowlist 外操作が必要と判断された（人間対応必要） |
| `report_log` | list[dict] | 送信済み逐次報告（送信失敗時は `delivered: false` で退避） |
| `degraded_mode` | bool | Runbook 未整備/取得失敗による縮退動作中フラグ |

**State Constraints（必須）:**
- Flat TypedDict のみ（primitive + JSON-serializable）。Pydantic / dataclass / 任意 Python オブジェクト禁止
- JWT / API key / **read-only credential を含む一切の認証情報**を State に入れない（checkpoint DB 漏洩）。
  診断対象への接続情報は tool 実行層（services）で `ctx.secrets` から都度 resolve し、戻り値にも載せない
- **手順書（Runbook）由来のテキストは credential scrub 済みのもののみ State / LLM prompt に載せる**
  （現場の手順書には平文 ID/PW が記載されていることが多い — 下記 §Runbook 決定木 DSL の scrub 要件）。
  **実際の接続情報は手順書からは決して取得せず、`ctx.secrets`（`DIAG_READONLY_CREDENTIALS`）のみから
  resolve する**（手順書記載の credential は仮に scrub を逃れても使用しない一方通行の原則）
- `tail_log` 等の tool 出力は **service 層で credential / PII を scrub してから返す**（tool_results は
  checkpoint に永続化されるため、汚染値を State に載せない — Cat 2 checkpoint 漏洩の教訓を tool 境界に適用）
- `InvocationContext` は `config["configurable"]` 経由のみ（State に入れない）

## Runbook 決定木 DSL（canonical・行動空間の定義）

決定木は機械可読な canonical DSL（YAML/JSON）で表現する。DSL が **think の行動空間そのもの**であり、
自律性はこの内側に閉じる（外側のノード・tool は選択肢に存在しない）。

```yaml
runbook_id: "svc-web-portal"          # 対象システム
version: "2026-07-01"                  # 版（source of truth は顧客所有）
match:                                 # アラート → この木を辿る条件
  target_system: "web-portal"
nodes:
  - id: "check-health-ep"
    description: "ヘルスエンドポイント疎通"
    tool: "http_get_healthcheck"       # allowlist 済み tool 参照（未登録 tool 参照はロード時に拒否）
    args: { target: "portal-health" }  # 実 URL は config 側 allowlist が解決（DSL に生 URL を書かない運用も可）
    on_ok:  ["check-login-reach"]      # 分岐候補（think が観測を見て選ぶ・順列強制ではない）
    on_ng:  ["check-db-liveness", "check-lb-metrics"]
severity_hint: {...}                   # 影響度の目安（think の優先度判断材料）
```

### 手順書の取得と実行時変換（導入時バッチ前提の廃止 — 2026-07-06 ユーザー判断）

手順書→DSL 変換を「導入時整備・agent スコープ外」とする前提は導入負担が重すぎるため廃止する。
現場の手順書は高頻度で更新される（正常性確認用 ID/PW が 1 ヶ月で期限切れになりローテーションされる等）
のが普通であり、事前変換 DSL は陳腐化 = 誤誘導リスクを抱える。**取得した最新の手順書を実行時に読む**
方式を基本とする。

- **取得（per_invocation）**: initialize が実行時に URL/ファイルから手順書を取得する — 常に最新版。
  RAG への固定投入は採らない（陳腐化 = 誤誘導リスク）。
- **実行時変換**: 取得した手順書が
  - (a) **既に valid DSL** → そのまま使用（変換スキップ）。人間確定済みの事前変換 DSL は
    **成熟顧客向けのオプション高速パス**（必須前提ではない）。
  - (b) **非構造テキスト**（Confluence / Wiki / Markdown / スプレッドシート由来テキスト等）→
    **initialize 内で LLM が決定木 DSL へ実行時変換**する（LLM 1〜2 call・トリアージ全体の
    応答時間内に収まる想定）。変換 LLM コストは `cost_usd` に計上され `budget_usd` の内数。
- **変換 LLM のモデル選定方針（レビュー指摘 F-02）**: 変換には**コンストラクタ注入される同一の
  `BaseLLM` インスタンスを既定で使用**する（追加のモデル管理・secrets を増やさない）。
  軽量モデルへ分離したい利用者向けに `runbook_conversion.llm`（optional・未指定時は主 LLM を使用）
  を config 拡張ポイントとして規定する。
- **変換キャッシュ（config `runbook_source.mode: cached`）**: 手順書コンテンツのハッシュ
  （`doc_hash`）をキーにした変換結果キャッシュ。文書が変更されればハッシュが変わり自動で
  キャッシュ無効化 = 更新へ自動追従（旧 B: cached_batch の「更新トリガで人手再投入」を置き換え）。
  キャッシュ実体は service 層（`src/services/` のプロセス内 LRU・`cache_max_entries` 上限）に置き、
  **State には載せない**（State へ入るのは採用された `decision_tree` と `runbook_ref` メタのみ）。
- **credential 混入対策（S-2 要件）**: 現場の手順書には平文 ID/PW が記載されていることが多い。
  **取得した手順書テキストは credential パターン scrub（credential scan ＋ `shared.security.pii_detector`）
  を通してから LLM prompt / `working_memory` に載せる**。除去箇所は `[REDACTED-CREDENTIAL]`
  プレースホルダに置換し、除去件数を監査イベントに記録する。生成された決定木 DSL にも credential を
  持ち込まない（tool 参照＋target 名のみ）。実際の接続情報は手順書からでなく `ctx.secrets`
  （`DIAG_READONLY_CREDENTIALS`）のみから resolve する。
- **変換後の検証パイプライン（従来どおり全適用）**: (a) DSL スキーマ検証、(b) 全ノードの `tool` が
  登録済み read-only tool 名であること、(c) 到達不能ノード・循環の検出、(d) instruction-like text
  検出（S-2）。違反は `SecurityViolationError` / 縮退モードで扱う。
  **設計根拠**: 5 層エンフォース（§Read-only 診断の多層物理エンフォース）は決定木の出来と独立に
  効くため、**変換ミスの最悪影響は「無駄な read-only 確認による budget 浪費」であり write には
  構造的に到達不能**。だからこそ実行時変換（人間の事前レビュー省略）を許容できる。
- **変換の透明性（人間レビュー省略の代償措置）**: 変換に使った手順書の版（`doc_hash`・取得時刻）と
  生成された決定木を**最終レポートに同梱**し、「AG が手順書をどう読んだか」を事後に人間が確認できる。
  逐次報告にも「決定木 N ノードを生成（手順書 vX / cache hit or miss）」の 1 行を出す。
- **S-4 イベント**: `runbook_fetched`（source, doc_hash）/ `runbook_converted`
  （node 数, 検証結果, credential 除去件数, cache hit/miss）を initialize の execute 内で emit。
- **縮退モード**: 手順書の取得失敗 / マッチする決定木が構成できない場合、診断は行わず「受信・対象特定・
  逐次報告＋『Runbook 未整備（該当決定木なし）』の明示」まで実施する（黙って何もしないより運用上有用）。

## Read-only 診断の多層物理エンフォース（安全設計の核）

「書き込み系操作を物理的に実行不可」を config の口約束にせず、**独立した 5 層**で強制する。
1 層が破られても残りの層が防ぐ defense-in-depth。

| 層 | 強制内容 | 実装箇所 |
|----|---------|---------|
| ① credential 層 | 診断対象への接続は顧客発行の **read-only 権限 credential のみ**（資格情報レベルで write 不能）。`ctx.secrets.require()` で tool 実行層が都度取得し State に載せない | `src/services/` ＋ agent.yaml `requires.secrets` |
| ② 実行境界層 | **protocol 共通の egress ゲート `src/services/readonly_gateway.py` に全外部到達を集約**し、そこ以外に外部接続コードパスを置かない。(a) HTTP: **GET / HEAD メソッドのみ実装**（POST/PUT/DELETE/PATCH はコードパスが存在しない）。(b) 非 HTTP tool も同ゲート経由 — `probe_connectivity` は connect-only の TCP handshake / ICMP echo のみ（アプリケーションデータの送信 API を持たない）、`tail_log` はログ参照 API への GET（同 HTTP client）または read-only 権限のログエンドポイント read に限定。すべての protocol で config の `target_allowlist`（host:port 単位）を照合し未登録宛先を拒否。デプロイ環境ではネットワーク ACL で同 allowlist を重畳 | `src/services/readonly_gateway.py`（HTTP/TCP/ログ read 共通） |
| ③ tool 登録層 | `get_tools()` が返すのは read-only 診断 tool のみ。**write 系 tool は tool schema に定義自体が存在しない**（LLM の呼び出し語彙に無い）。LLM が未知 tool 名を幻覚しても `ToolActNode` が未登録 tool として拒否 | `src/tools/` ＋ `get_tools()` |
| ④ allowlist 更新ガバナンス | `target_allowlist`・tool 追加は config 変更 = Git MR + 人間レビューを必須とする（agent が実行時に自己拡張できる経路が無い） | config 管理プロセス |
| ⑤ 監査層 | 全診断実行を S-4 `emit_trace_event("diagnostic_executed", {tool, target, verdict}, state)` で記録。逐次報告 push も `report_pushed` で記録 | 各 execute() |

> 検証要件（`docs/03_test_spec.md` へ引き継ぐ）: 「write 操作が実際に実行不能」を PB テストで実証する
> — (a) http client に GET/HEAD 以外が存在しないこと、(b) 未登録宛先 / 未登録 tool の呼び出しが拒否
> されること、(c) tool_results / checkpoint に credential が残らないこと。

### Tools（`src/tools/` — すべて read-only）

| tool | 役割 | 安全境界 |
|------|------|---------|
| `http_get_healthcheck(target)` | 承認済みヘルス EP へ HTTP GET（200-399 = 健全） | ②GET のみ・target_allowlist |
| `probe_connectivity(target)` | ping / TCP ポート疎通 / TLS 到達確認 | ②allowlist 宛先のみ |
| `read_metric(target, metric, window)` | 監視系 API からメトリクス値を GET | ①read-only cred・②GET のみ |
| `tail_log(target, lines)` | ログの read（参照のみ）。**返却前に credential/PII scrub** | ①read-only cred・State 汚染防止 |
| `check_liveness(target)` | 死活監視 API GET・ログイン画面到達確認（認証実行はしない・到達のみ） | ②GET のみ |
| `record_hypothesis(cause, confidence, evidence)` | **内部 tool（外部副作用なし）** — 原因仮説と確度を `working_memory.hypotheses` へ記録。確度収束④の機械可読化。**戻り値は `tool_results` に追記され checkpoint に永続化されるため、`cause` / `evidence` は診断サマリ用の非秘匿テキストとして設計し credential / PII を含めない**（S-5 準拠。`evidence` は `tail_log` 等の scrub 済み観測の `node_id` 参照のみとし、生ログ・接続情報を書かない） | 外部到達なし（readonly_gateway を経由しない唯一の tool） |

tool は `.name` を持ち `(**args)` 呼び出し可。戻り値は msgpack-safe な dict（State へ載るため）。
外部到達を伴う tool はすべて `readonly_gateway` 経由。`SELECT 1` 相当を超える DB クエリ・
状態変更系はいずれの tool にも存在しない。

## 逐次報告（incremental reporting — 価値の一部）

- **実現方式**: LLM に「報告 tool を呼ばせる」方式は採らない（呼び忘れで報告が欠落する）。
  **`TriageActNode` が診断 tool 実行のたびに機械的に 1 行報告を push** する（確実性を構造で担保）。
- **報告フォーマット**（短く事実ベース・緊急 cadence）:
  `[TEL-C3-909] web-portal: 手順3 ヘルスEP疎通 → NG (HTTP 503) | 確認済 3/8 | 次候補: DB死活`
- **報告先**: Slack/Teams 等の ChatOps webhook。**URL は秘匿値として secrets が正本** —
  config には secret キー名だけを置き（`reporting.webhook_secret_key: "CHATOPS_WEBHOOK_URL"`）、
  act の報告 push 時に `ctx.secrets.require(<キー名>)` で resolve する（URL を config / State に書かない）。
- **push 失敗時**: リトライ 1 回 → 失敗しても診断ループは止めず `working_memory.report_log` に
  `delivered: false` で退避し、最終レポートに未達分を同梱する（報告インフラ障害で切り分けが
  止まらない）。
- **決定木生成の透明性 1 行**: initialize が手順書を実行時変換した場合、診断開始前に
  「`[TEL-C3-909] web-portal: 決定木 8 ノードを生成（手順書 v doc_hash 先頭 8 桁 / cache miss）`」を push
  し、オペレータが「どの版の手順書に基づく診断か」を対応中に把握できるようにする。
- 冗長な中間思考（thoughts 全文）は流さない。観測事実＋進捗カウンタのみ。

## 最終出力（assemble_output — オペレータが数秒で読める形）

アラート対応中のオペレータは余裕がない。最終レポートは**先頭 3 行で意思決定できる**構造にする:

```
【状況】一次切り分け 完了（8 確認中 7 実施・確度 0.82）/ 打ち切り（budget 到達）
【最有力仮説】DB コネクションプール枯渇（確度 0.82）— 根拠: 手順3 NG(503)・手順5 メトリクス閾値超過
【次アクション】DB 再起動判断は L2 へエスカレーション要（allowlist 外操作のため人間対応必須: Yes）
```

続けて詳細セクション:

| セクション | 内容 |
|-----------|------|
| `summary` | 上記 3 行（status / top_hypothesis / next_action + escalation_required） |
| `checked_matrix` | 実施した確認の一覧 `{手順, 対象, 結果(ok/ng/failed), 時刻}` — **「ここまでは白」を明示** |
| `unchecked` | 未確認の枝（打ち切り時に何が残っているかを隠さない） |
| `hypotheses` | 原因仮説（**必ず「仮説＋確度＋根拠」形式・断定文言は生成しない**） |
| `recommended_actions` | 推奨対処（実行は人間）＋ allowlist 外操作の明示 |
| `incident_report_draft` | インシデント報告ドラフト（顧客の報告書式に沿った Markdown） |
| `report_log` | 送信済み逐次報告＋未達分 |
| `runbook_trace` | **変換の透明性** — 使用した手順書の版（doc_hash・取得時刻・cache hit/miss・credential 除去件数）と生成された決定木 DSL。「AG が手順書をどう読んだか」を事後に人間が検証できる |

- **打ち切り時（budget 枯渇 = cancelled / max_iterations）も同一構造で必ず出力**する。
  `checked_matrix`（白判定済み）と `unchecked`（残り）を明示し「中途半端に黙る」ことをしない。
  status は cancelled のまま保全（無条件 SUCCESS 禁止）。
- 出力言語は config `output.language`（既定 `ja`）。逐次報告・最終レポートとも同一言語。

## Framework Utilization

### Shared Components Used
- [x] InvocationContext（correlation_id, session_id, permissions, credential handle — `config["configurable"]` 経由）
- [x] `shared.services.llm.base_llm.BaseLLM`（コンストラクタ注入。注入シームは `src/services/llm_client.py`・テストは fake 注入）
- [x] SecurityViolationError（DSL 検証違反・allowlist 逸脱時）
- [x] **S-2**: `_extra_security_gate_input()` — `TriageInitializeNode` / `TriageActNode` に実装。
      (a) アラート JSON のスキーマ・サイズ上限検証、(b) **prompt-injection 対策** — アラートの
      annotation / label 等の自由文字列フィールドは「観測データ」としてマーキングし、think prompt では
      データと指示を構造的に分離（アラート本文中の命令文を指示として扱わない）。**Runbook DSL の自然文
      フィールド（`description` / `severity_hint` 等）も同じく顧客提供の外部入力であり同一の扱い** —
      観測データとしてマーキングして指示と分離し、DSL ロード時の静的検証で命令文パターン
      （「無視して」「実行して」等の instruction-like text）を検出したら flag / 拒否する
      （決定木自体を注入経路にしない）、(c) 未知 runbook 参照の拒否、(d) **手順書テキストの
      credential scrub** — 取得した手順書は credential パターン scrub（credential scan ＋
      `shared.security.pii_detector`）を通してから LLM prompt / working_memory へ。除去箇所は
      `[REDACTED-CREDENTIAL]` に置換し除去件数を S-4 監査イベントに記録（§Runbook 決定木 DSL）。
      `_security_gate_input()`（`@final`）は override しない。
- [x] **S-3**: `_extra_security_gate_output()` — `TriageFinalizeNode` に実装。
      (a) 原因仮説の断定表現ブロック（「原因は◯◯です」等 → 仮説形式へ強制）、(b) evidence の無い仮説を
      「要確認」へ降格、(c) レポート内 credential/PII 残存の最終検証、(d) escalation_required=true の場合に
      recommended_actions へ人間対応必須の明示が含まれることの検証（preservation variant）。
      `_security_gate_output()`（`@final`）は override しない。
- [x] **S-4**: `from shared.utils.audit_logger import emit_trace_event` を custom node の execute 内で
      ドメインイベントとして最低 1 回 emit — `alert_received` / `runbook_fetched`（source, doc_hash）/
      `runbook_converted`（node 数, 検証結果, credential 除去件数, cache hit/miss）/
      `runbook_matched`（initialize）、`diagnostic_executed` / `report_pushed`（act）、
      `triage_assembled`（finalize）。
      `node_start` / `node_complete` / `node_error` は `BaseNode.__call__` が自動 emit ＝ 重複禁止。
      think は framework default `ThinkNode`（framework 側で LLM call イベントを emit）。

### Composition Pattern

- **Pattern**: Standalone（自律ループ単体。GraphNode / RemoteAgentNode は使わない）
- **Error propagation strategy**: tool 例外 → `ToolActNode` 系が `tool_results` に failed として記録し
  ループ継続（retry-vs-代替は think の判断②）。ノードレベル error → `status=error` →
  `_route_after_think` が finalize へ → finalize は status を保全しつつ部分結果＋エラーを載せて返す（パススルー）。

## Security Model（S-1〜S-5）

### S-1 — Trust Level
- **Agent 全体の最小 `required_trust_level` = `VERIFIED_EXTERNAL`**（`config/agent.yaml` 宣言）。
  根拠: 顧客本番システムの診断情報・障害情報を扱うため匿名呼び出しを許可しない。
- **顧客本番へ実アクセスする `TriageActNode` は `required_trust_level = INTERNAL`** を class body で宣言。
  根拠: read-only とはいえ本番到達性を持つ診断実行は、信頼された内部経路（監視 webhook receiver 等の
  内部システム）からのみ許可する多層防御。`TriageInitializeNode` / `TriageFinalizeNode` は
  `VERIFIED_EXTERNAL`。全 custom node で class body に明示宣言（暗黙 ANONYMOUS 継承は不可）。
- **呼び出し元契約（caller contract — 明示仕様）**: 標準運用の呼び出し元は監視 webhook receiver
  （信頼された内部システム）であり **`INTERNAL` で invoke する**。これが診断ループのフル実行が
  成立する唯一の経路。`VERIFIED_EXTERNAL` の呼び出しでは initialize / think は動作するが、
  `TriageActNode` の S-1 ゲートが実行を拒否（`status=error`）→ finalize が「受信・対象特定・
  未実施の確認一覧＋『診断実行は INTERNAL 経路のみ』」の部分レポートを返す。これは矛盾ではなく
  **定義済みの縮退挙動**（診断なしの一次特定レポート）としてテスト仕様（TC-08 系）で検証する。
- 有効な TrustLevel は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` の 3 値のみ（他の値は import 時に AttributeError）。

### S-2 — 入力ゲート
- framework `@final` の既定 PII scan が全 FunctionNode で自動実行。
- ドメイン S-2 = 上記 `_extra_security_gate_input()`（スキーマ/サイズ検証・prompt-injection 分離・
  未知 runbook 拒否）＋ DSL ロード時検証（未登録 tool 参照・循環の拒否）。

### S-3 — 出力ゲート
- framework `@final` の既定 credential scan が自動実行。
- ドメイン S-3 = `TriageFinalizeNode._extra_security_gate_output()`（断定ブロック・根拠なし仮説の降格・
  credential/PII 最終検証・エスカレーション明示の preservation verification）。
- 加えて **tool 境界 scrub**（`tail_log` 等が返却前に credential/PII を除去）で checkpoint 汚染を予防。

### S-4 — 監査
- 上記ドメインイベント 5 種を execute 内で emit。診断 1 件 = `diagnostic_executed` 1 件が必ず残る
  （多層エンフォース⑤）。framework 自動 emit と重複させない。

### S-5 — 認証情報
- 秘匿値は `ctx.secrets.require()` のみ（`os.environ` 禁止）。`requires.secrets` に全宣言（下記）。
- `CHATOPS_WEBHOOK_URL` は `config.reporting.webhook_secret_key` に対応するキー名であり、act の報告 push
  実装は `ctx.secrets.require(config.reporting.webhook_secret_key)` で resolve する（§逐次報告と同一仕様）。
- `src/` に credential を置かない。テストは明示 mock 値（例 `"mock-readonly-cred-for-testing"`）。
- pyproject の依存はすべて `==` 完全 pin（gate-dep-pinning）。

## config/agent.yaml 主要キー設計値

```yaml
agent:
  id: "TEL-C3-909"
  name: "MonitoringAlertFirstTriageAgent"
  category: "Cat 3"
  industry: "TEL"
  base_type: "AutonomousBaseGraph"   # L1 base class; conceptual pattern: ReAct
  module: "src.graph"
  class: "MonitoringAlertFirstTriageAgent"
  config:
    # ⚠ budget_usd / max_iterations / confidence_threshold は「テンプレ利用者が最終的に
    #   自環境（決定木の実深さ・アラート量・コスト方針）に合わせて設定する前提の初期値」。
    #   初期値の根拠は仮置きであり PoC 実測で調整する（2026-07-06 確定）。
    budget_usd: 1.50                 # REQUIRED — invocation あたり LLM コスト hard ceiling。
                                     #   手順書→DSL の実行時変換 LLM コストもこの内数として計上される。
                                     #   算出根拠（概算）: 決定木変換 1〜2 call ≈ $0.1〜0.3 +
                                     #   think ループ max_iterations=24 上限（1 call あたり数セント想定）
                                     #   の合算に安全余裕を加えた初期値。PoC 実測で調整する前提
    max_iterations: 24               # 決定木深さ＋retry 余裕（DEFAULT 50 より安全側）
    confidence_threshold: 0.75       # think の収束判断材料（④）
    runbook_source:
      mode: "cached"                 # per_invocation（毎回取得・変換）/ cached（doc_hash キー変換キャッシュ・
                                     #   文書変更で自動無効化 = 更新へ自動追従）
      location: ""                   # 顧客ごと設定（URL / パス）
    runbook_conversion:
      enabled: true                  # 非構造手順書の実行時 LLM 変換（false = valid DSL のみ受理する高速パス運用）
      cache_max_entries: 64          # 変換キャッシュ上限（doc_hash キー）
    target_allowlist: []             # 診断可能宛先（host:port）。空 = 縮退（診断なし）
    reporting:
      enabled: true
      webhook_secret_key: "CHATOPS_WEBHOOK_URL"  # 報告先 URL は secrets が正本（config はキー名のみ）
      min_interval_seconds: 0        # 逐次報告の cadence 調整（0 = 毎観測）
    output:
      language: "ja"
    input_adapters: ["alertmanager", "datadog", "pagerduty", "generic"]
  memory_enabled: false              # v0。相関 invocation 跨ぎ共有を有効化する場合 true + correlation_key 運用
  hitl:
    enabled: false                   # 実行時 HITL なし（read-only のみで承認対象操作が存在しない。
                                     # allowlist 更新の人間承認は Git MR ガバナンス④で担保）
  required_trust_level: "VERIFIED_EXTERNAL"
  requires:
    secrets:
      - "DIAG_READONLY_CREDENTIALS"  # 診断対象への read-only 接続情報（JSON map・tool 層で resolve）
      - "CHATOPS_WEBHOOK_URL"        # 逐次報告先 webhook
```

> `llm` は `BaseLLM` インスタンスとしてコンストラクタ注入（`src/api/server.py` が agent.yaml を読んで
> 構築・`src/services/llm_client.py` が注入シーム）。`budget_usd` / `llm` 欠落は compile() 時 ConfigError。

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: `framework/` と `shared/` のみ（`agents/base/` 不要・他テンプレート import なし）

## ユーザビリティ設計レビュー（実施記録）

エンドユーザー = アラート対応中で余裕のない NOC オペレータ / SRE。以下を自己レビューし設計へ反映した:

| 観点 | レビュー結果 → 修正した点 |
|------|------------------------|
| 入力の自然さ | 当初案は「顧客側で専用 JSON へ整形」のみだった → **代表 3 形式（Alertmanager / Datadog / PagerDuty）の raw webhook payload をそのまま受ける input adapter を initialize に追加**（`input_adapters`）。その他の監視系のみ generic mapping 設定が必要。導入障壁を下げる |
| 出力の即応性 | レポートが詳細一辺倒だと緊急時に読めない → **先頭 3 行サマリ（状況 / 最有力仮説 / 次アクション＋エスカレーション要否）を必須構造化**。数秒で意思決定できる形を仕様化 |
| 打ち切り時の透明性 | budget/iterations 打ち切りで出力が中途半端になる懸念 → **cancelled でも必ず `checked_matrix`（ここまでは白）＋ `unchecked`（未確認の枝）を明示する部分レポートを仕様化**。「どこまで確認済みか」を隠さない |
| 逐次報告の確実性 | LLM に報告 tool を呼ばせる案は呼び忘れリスク → **act ノードが機械的に push する構造に変更**（LLM の裁量に依存しない）。ノイズ回避のため 1 行事実ベース＋`min_interval_seconds` で cadence 調整可 |
| 外部依存の失敗時 | Runbook 取得失敗 / 報告 webhook 失敗 / tool 失敗 → それぞれ **縮退モード（受信・特定・報告のみ＋「Runbook 未整備」明示）/ report_log 退避＋最終レポート同梱 / think の retry-vs-代替判断（②）** としてユーザーに何が返るかを定義。黙って失敗しない |
| 対話（壁打ち） | 本ユースケースは緊急一次対応であり、対話 refine より「速く・確実に・読める形で」が価値。実行時 HITL は導入せず（read-only のみで承認対象が無い）、結果を見た人間の追加調査指示は新規 invocation として扱う設計とした |
| 導入障壁（手順書整備） | 当初設計は「非構造手順書→DSL 変換は導入時バッチ＋人間確定（agent スコープ外）」で、導入顧客に DSL 整備を要求していた → **実行時 LLM 変換に変更し導入時整備を不要化**（2026-07-06 ユーザー判断）。手順書はそのまま（Confluence/Wiki/Markdown）で導入でき、credential ローテ等の高頻度更新にも自動追従する。人間レビュー省略の代償は runbook_trace の事後検証性と検証パイプライン＋5 層エンフォースで担保 |

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AutonomousBaseGraph** | Cat 3 の観測駆動の経路選択・自律終了は固定パイプラインで書けない |
| finalize | default FinalizeNode（CASE 1） | custom TriageFinalizeNode（CASE 2） | **custom（CASE 2）** | wheel 実体では `assemble_output()` が呼ばれない既知問題。custom finalize で呼び出し＋status 保全＋S-3＋S-4 を担う |
| 決定木の表現 | 事前バッチ変換＋人間確定 DSL のみ | canonical DSL＋**実行時 LLM 変換**（事前確定 DSL は optional 高速パス） | **canonical DSL＋実行時 LLM 変換** | 手順書は高頻度更新（確認用 ID/PW の月次ローテ等）が現場の普通であり、事前変換 DSL は陳腐化 = 誤誘導リスク。導入時整備の負担も重い。変換後も検証パイプライン＋5 層エンフォースが独立に効き、変換ミスの最悪影響は budget 浪費（write 到達不能）。透明性は runbook_trace 同梱で担保（2026-07-06 ユーザー判断） |
| 逐次報告の起点 | LLM が report tool を呼ぶ | act ノードが機械的に push | **act ノードが機械的に push** | 呼び忘れによる報告欠落を構造で排除（usability レビュー反映） |
| 相関アラート共有 | 外部キャッシュ服務で invocation 跨ぎ共有 | 同一 group payload 内共有（+ memory オプション） | **同一 payload 内共有を v0 基本形** | Alertmanager 等の group 通知が業界標準で 1 payload に相関群が届く。外部キャッシュは複雑性増・v0 スコープ外（memory_enabled + correlation_key を将来オプションとして規定） |
| write 不能の担保 | config で禁止宣言 | 多層物理エンフォース（5 層） | **多層物理エンフォース** | config は口約束。credential / コードパス不在 / tool schema 不在 / ガバナンス / 監査の独立 5 層で defense-in-depth |
| 実行時 HITL | interrupt() で対処承認 | HITL なし（read-only 完結＋人間エスカレーション） | **HITL なし** | 実行できる操作がすべて read-only で承認対象が存在しない。対処実行は本 agent のスコープ外（人間）。allowlist 更新の承認は Git MR ガバナンスで担保 |

## Open Items
- `assemble_output()` が framework から未呼び出しの件は custom finalize で吸収。
  framework sample と released wheel の整合は上流の課題とし、本テンプレートは現行パターンを維持する。
- 運用監視は業種非依存のため共通カテゴリへの振替も検討したが、**`TEL-C3-909` 維持で確定
  （Template ID / プロジェクト名の変更コストが利得を上回るため振替せず）。本項はクローズ扱い**。
- **`docs/07_operation_guide.md` に記載する運用項目**:
  1. **オペレータ手動再トリアージの導線** — S-1 caller contract（診断フル実行は INTERNAL 経路のみ）の
     帰結として、結果を見たオペレータが追加調査・再実行したい場合の手順 =
     **webhook receiver 経由の再発火手順**を運用ガイドに明記する（オペレータが直接 invoke する経路は無い）。
  2. **target_allowlist 登録の運用ルール** — 「read-only credential を発行できない対象システムは
     `target_allowlist` に載せない」を運用ルールとして明記する（多層エンフォース①が成立しない対象を
     診断対象にしない）。
- 手順書→DSL の**実行時 LLM 変換への設計変更**: 当初の Proposal は
  「導入時バッチ＋人間確定」前提で記載されている。本 Design で実行時変換
  （＋検証パイプライン＋runbook_trace 透明性＋credential scrub）へ更新した。差分は本書
  §手順書の取得と実行時変換 / Design Decision Record を正とする。
