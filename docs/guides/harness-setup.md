# ハーネス導入ガイド（必須スキル・必須 MCP・pstack）

このガイドでは、共通の開発ハーネス（pstack）と対象固有のハーネスに必要なスキルおよび MCP サーバーを、ローカル環境（Antigravity / Cursor / Claude Code / Codex）および GitHub Actions CI へ再現性高く導入・検証する手順をまとめます。

前提となる責務分担と宣言スキーマは [詳細設計書 §7.2](../design/detailed-design.md#72-共通の開発ハーネスpstackマニフェストharnessespstacktoml) を参照してください。

---

## 1. 不足物を診断し、環境別の導入手順を出す

対象リポジトリのルートで次を実行します。

```sh
# 構成と必須スキル・必須 MCP の設定を診断する（--skip-network は GitHub ブランチ保護 API の確認のみ省略）
garden doctor --target . --skip-network

# observe_only でも pstack コアスキルを必須として診断し、MCP の実接続（initialize）まで検証する
garden doctor --target . --strict-harness --probe-mcp

# プロジェクト内設定（.mcp.json / .codex/config.toml 等）の stdio MCP コマンドを内容確認のうえ起動許可する場合
garden doctor --target . --strict-harness --probe-mcp --trust-project-mcp

# 不足しているスキル・MCP とテンプレート推奨パターン、環境別の設定スニペットを表示する
garden harness guide --target . --adapter all
```

`--skip-network` は GitHub のブランチ保護 API 確認だけを省略するオプションであり、`--probe-mcp` の HTTP/SSE 疎通確認は無効化しません。また、プロジェクト内設定ファイル由来の stdio コマンド・チェックアウト内実行ファイル・認証設定付き HTTP プローブはセキュリティのため既定では実行せず、内容確認後に `--trust-project-mcp` を明示したときだけ実行します。特定のアダプターだけを確認したいときは `--adapter antigravity|cursor|claude|codex` を指定します。

---

## 2. 固定版 pstack を同期する（`garden harness sync-pstack`）

`harnesses/pstack.toml` に記録された `pinned_ref`（40 桁コミット SHA）を検証したうえで pstack を導入できます。既定はドライランで、実際の書き込みは `--apply` を付けたときだけ行います。

```sh
# ドライランで取得元・固定 SHA・配置先を確認する
garden harness sync-pstack --adapter antigravity

# 環境ごとに固定版 pstack を導入する
garden harness sync-pstack --adapter antigravity --apply   # ~/.gemini/config/plugins/pstack（プラグイン構成）
garden harness sync-pstack --adapter cursor --apply        # ~/.cursor/plugins/pstack（プラグイン構成）
garden harness sync-pstack --adapter codex --apply         # ~/.agents/skills（スキル展開・既存スキル保持）
garden harness sync-pstack --adapter claude --apply        # ~/.claude/skills（スキル展開・既存スキル保持）
garden harness sync-pstack --adapter gemini --apply        # ~/.gemini/skills（スキル展開・既存スキル保持）
garden harness sync-pstack --adapter repo --target . --apply         # <repo>/.agents/skills（Codex / Antigravity 向け同梱）
garden harness sync-pstack --adapter claude-repo --target . --apply  # <repo>/.claude/skills（Claude Code 向け同梱）
garden harness sync-pstack --adapter gemini-repo --target . --apply  # <repo>/.gemini/skills（Gemini CLI 向け同梱）
```

`sync-pstack --apply` は取得したコミットが `pinned_ref` と完全一致すること、および `[skills].core` の全スキルに `SKILL.md` があることを確認してから、全同期先を事前検査・ステージングしたうえで一括置換します。配置先や対象スキルがシンボリックリンクの場合は参照先を破壊しないよう拒否し、管理外の既存同名スキルは `--force` なしでは上書きしません（中断や復元失敗時は旧バックアップを削除せず保護します）。`--approval-branch main` と `--flow <flow>` を付けると、承認済み `[common_harness].ref` の照合と実行フローのアダプター限定同期を行います。なお、相対パスの `--dest` は呼び出し元のカレントディレクトリ基準で解決します。

---

## 3. 環境別の探索パスと MCP 設定

| 環境（アダプター） | スキル探索先（既定） | MCP 設定ファイル（下位 → 上位の優先順） |
| --- | --- | --- |
| **Antigravity** | `~/.gemini/config/plugins/pstack`, `~/.gemini/config/skills`, `.agents/skills` | `~/.gemini/antigravity/mcp` → `~/.gemini/config/mcp_config.json` → `~/.gemini/antigravity/mcp_config.json` → `.mcp.json` |
| **Cursor** | `~/.cursor/plugins/pstack`, `~/.cursor/skills`, `.cursor/skills`, `.agents/skills` | `~/.cursor/mcp.json` → `.cursor/mcp.json` |
| **Claude Code** | `~/.claude/skills`, `.claude/skills`, `.agents/skills` | `~/.claude.json`（全体） → `.mcp.json` → `~/.claude.json`（対象プロジェクトの `mcpServers` / `disabledMcpjsonServers`） |
| **Gemini CLI** | `.agents/skills`, `.gemini/skills`, `~/.agents/skills`, `~/.gemini/skills`（同名スキルは `.agents/skills` 優先、`skills.enabled` / `skills.disabled` を反映） | `~/.gemini/settings.json` → `.gemini/settings.json`（`httpUrl`・`mcp.allowed`・`mcp.excluded` を反映） |
| **Codex** | `~/.agents/skills`, `.agents/skills` | `~/.codex/config.toml` → `.codex/config.toml` |

### MCP サーバー設定の判定ルール

- サーバー設定はオブジェクトであり、空でない文字列の `command`・`url`・`serverUrl`・`httpUrl`（Gemini CLI の Streamable HTTP）のいずれかが必要です（`null` や空オブジェクトは未設定扱い）。
- 上位設定に `disabled = true` / `enabled = false` / `disabledMcpjsonServers`、または Gemini CLI の `mcp.excluded` / `mcp.allowed` による除外があるサーバーは無効化済み（不足）として扱われます。
- 実エージェントを使うフローでは、そのフローで選択したアダプター（例: `codex` / `gemini`）に適用される MCP 設定だけを参照します。
- `garden doctor --probe-mcp` を指定すると、stdio サーバーには MCP `initialize`（JSON-RPC 2.0）を送信し、HTTP/SSE サーバーには `initialize` POST（400 / 404 / 405 時は旧 HTTP+SSE の `endpoint` イベント経由で `initialize`）を送信して、`protocolVersion`・`capabilities`・`serverInfo.name` を含む応答を検証します。Codex の `bearer_token_env_var` / `http_headers` / `env_http_headers` も反映し、別オリジンや HTTP ダウングレードへのリダイレクト時は認証ヘッダーを転送・復元しません。

### Gemini CLI の認証経路・非対話実行・コスト上限の扱い

- **利用可能な認証経路**: 個人向け Google AI Pro / Ultra の `Login with Google` 経由による Gemini CLI 利用は 2026 年 6 月 18 日に終了しています（[Google の提供終了告知](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals)）。現在は次のいずれかの認証経路を使用してください。
  1. **Gemini Developer API キー**: 環境変数 `GEMINI_API_KEY` を設定する。
  2. **Vertex AI**: `GOOGLE_GENAI_USE_VERTEXAI=true`・`GOOGLE_CLOUD_PROJECT`・`GOOGLE_CLOUD_LOCATION` と ADC 認証を設定する。
  3. **法人向け Google Workspace / Code Assist 管理アカウント**（組織契約が有効な場合）。
  - 個人アカウントで Google 製モデルをローカル CLI から使いたい場合は、Antigravity CLI（`adapter = "antigravity"`）も選択できます。
- **非対話実行の例**: `.garden/garden.toml` の `[agents.<name>]` では標準入力からプロンプトを受け取り JSON エンベロープを返す形（例: `command = ["gemini", "-p", "Follow the task instructions from stdin.", "-o", "json", "--approval-mode", "auto_edit"]`）で定義します（`command` の各要素は空文字不可）。
- **費用不明時のコスト上限検証**: Gemini CLI の `-o json` 出力には `total_cost_usd` が含まれないため、Garden は利用量を `0.0` に丸めず「不明（`cost_usd: null` / `cost_unknown: true`）」として記録します。既定では費用上限を検証できないため事後ガードが `NEEDS_HUMAN` で停止します。コスト上限を厳格に検証したい場合は費用を算出・返却するラッパー（または `total_cost_usd` を返す Claude Code 等）を使い、定額・外部上限付きの環境で再試行回数と時間上限（`max_retries` / `max_duration_sec`）により制御する場合のみ `[agents.<name>]` に `allow_unknown_cost = true` を明示してください（`max_cost_usd` との併用は設定エラーになります）。

### 書き込みモード（`propose_pr` / `auto_merge`）の `allowed_paths` 必須化への移行手順

`mode != "observe_only"` の設定では、既定委譲・フロー上書きとも空の `allowed_paths` を設定エラー（終了コード `2`）として拒否します。承認ブランチ（例: `main`）に `allowed_paths = []` の書き込み設定が残っている状態で `nursery_ref: main` を必須チェックに使うと、PR 側で `allowed_paths` を追加しても Trust Anchor が承認ブランチ側を新規則で読み直して終了コード `2` で停止します。次の順序で安全に移行してください。

1. `nursery_ref` を旧タグ（例: `v0.1.0`）に固定した状態、またはガード更新前の版で、承認ブランチ上の `.garden/garden.toml` に具体的な `allowed_paths`（例: `["src/**", "tests/**"]`）を反映してマージする。
2. 承認ブランチ側が新規則を満たしたことを確認したあとで、`nursery_ref` を最新版（`main` または新タグ）へ進める。

---

## 4. テンプレート別の標準ハーネス構成

`garden init --with-standard-harness` を付けて初期化すると、テンプレートの `[standard_harness]` に定義された標準の `required_skills` / `required_mcp` が `.garden/garden.toml` へ反映されます（フラグ省略時は空配列で初期化され、初期の `observe_only` CI でそのまま通ります）。

| テンプレート型 | 標準 `common_harness.required_skills` | 標準 `target_harness.required_mcp` | 対象固有のハーネス方針 |
| --- | --- | --- | --- |
| `cli` | `create-verification-skill`, `maintain-verification-skill` | なし | 終了コード・標準出力/エラー出力・隔離一時ディレクトリでの副作用を実コマンドで検証 |
| `web-frontend` | `create-verification-skill`, `maintain-verification-skill` | `playwright` | 実ブラウザ操作（Playwright MCP / Playwright Test）で表示・操作・永続化を検証 |
| `http-api` | `create-verification-skill`, `maintain-verification-skill` | なし | 実サーバー起動またはテストクライアントで正常・異常ステータスとデータ変化を検証 |
| `library` | `create-verification-skill`, `maintain-verification-skill` | なし | 公開 API 契約・型定義・後方互換性を利用者視点のインポート試験で検証 |

対象固有のスキルを追加したい場合は、対象リポジトリの `.agents/skills/<スキル名>/SKILL.md`（Claude Code を利用する場合は `.claude/skills/<スキル名>/SKILL.md`）を作成してコミットし、`.garden/garden.toml` の `[target_harness].required_skills` へ追記します。`garden run` は実行用ワークツリー（対象コミットのスナップショット）上で要件を検査するため、コミット済みのスキルと設定がそのまま CI でも再現されます。

---

## 5. GitHub Actions CI での再現

実エージェントまたは pstack 必須スキルを含むワークフローを GitHub Actions 上で動かすときは、`sync_pstack` を含むナーサリー版（`main`、または `sync_pstack` 導入後に発行したタグ）を `uses` と `nursery_ref` の両方に指定して [garden-reusable.yml](../../.github/workflows/garden-reusable.yml) を呼び出します。

```yaml
jobs:
  garden:
    uses: s-hiraoku/hiraoku-software-garden/.github/workflows/garden-reusable.yml@main
    with:
      flow: auto
      nursery_ref: main
      sync_pstack: true
```

`sync_pstack: true` を有効にすると、`garden run` の実行前に `garden harness sync-pstack --adapter auto --target . --approval-branch <承認ブランチ> --apply` が走り、承認済み `[common_harness].ref` と `pinned_ref` の一致を確認したうえで、フローの使用アダプターに応じたスキル配置先（Codex は `~/.agents/skills`、Claude は `~/.claude/skills` 等）へ固定版 pstack を配置します。

---

[ナーサリーとガーデンの使い方](usage.md) · [詳細設計書 §7.2](../design/detailed-design.md#72-共通の開発ハーネスpstackマニフェストharnessespstacktoml) · [現在の MVP 設計](../evolution/current-mvp.md) · [MVP 成長プラン](../evolution/roadmap.md)
