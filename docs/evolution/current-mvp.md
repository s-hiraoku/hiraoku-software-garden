# 現在の MVP 設計（MVP-4 完了時点）

この文書は、[概要設計書](../design/overview.md) と [詳細設計書](../design/detailed-design.md) に定義した**最終形**に対して、**現在の MVP（MVP-4 完了時点）でどこまで実装されており、何を意図的に割り切っているか**を記録する設計スナップショットである。

今後の段階的な進化計画は [MVP 成長プラン](roadmap.md) を参照する。

---

## 1. 現在地サマリー

- **現在の到達段階**: **MVP-4 完了**
- **稼働実績**
  - **ナーサリー自身（自己ドッグフーディング）**: `.garden/garden.toml` と `docs/specs/nursery-cli.md` により、[Garden Self-Dogfood](../../.github/workflows/garden-dogfood.yml)（本体検証と `reusable-smoke`）が `main` push、手動実行、および `.garden/**`・`docs/specs/**`・対象ワークフロー・形式検証スキルを変更する PR で `SATISFIED` として稼働している。
  - **1 号目の実対象リポジトリ（HAB）**: `s-hiraoku/hiraoku-agent-base` に `templates/cli/v1` を導入し、ローカル実行および実 GitHub Actions（PR #291・`workflow_dispatch`）で MVP-4 完了判定の 3 条件を実証した（[HAB 実証記録](evidence/mvp4-hab/README.md)）。
  - **形式検証**: `python3 .agents/skills/formal-verify-garden-engine/scripts/verify_invariants.py` で、決定論的ステートマシン・結実ゲート・委譲厳格化・反映ゲートの 8 不変条件（61,842 状態）を検証している。

---

## 2. 最終形（7 工程）と現在の MVP の対照

| 工程 | 最終形の姿（`docs/design/`） | 現在の MVP（MVP-4）での実装 | 現在の割り切り・未到達点 |
| --- | --- | --- | --- |
| **① 仕事の発見・受付** | Issue、ラベル、PR、スケジュール、CI 失敗など多様なイベントを受け、重複と対象を照合してフローを起動する | `[[triggers]]` の `auto` 解決、`garden-reusable.yml`（PR・PR 上の `/garden resume`・`workflow_dispatch`）、ローカルと HTML マーカーの冪等スキップを実装済み | 実運用は PR と手動実行が中心。`/garden resume` は PR 専用（非 PR の Issue は不可、権限者限定）であり、Issue 起票・`garden:ready` ラベル・CI 失敗起点の自動ワークフローは未実証 |
| **② 仕様化** | 人が承認した仕様の原本と委譲範囲を基準に、改定時は影響差分を解析して仕事を組み替える | `docs/specs/*.md` のフロントマターと `AC-*` / `INV-*` 抽出、保護ブランチ基準の Trust Anchor 照合、`garden spec-diff` を実装済み | `spec-diff` は CLI として実装済みだが、仕様改定 PR 時の自動影響再検証ワークフローへの組み込みは MVP-7 で扱う |
| **③ 実行管理** | 必須スキルと必須 MCP を含むハーネス環境を検証したうえで、着手順・分割・対象の版・権限を管理して隔離環境で実行する | 宣言的ステートマシン、`git worktree` 隔離と `verified_commit` 永続化、委譲厳格化バリデーターを実装済み | 必須スキルと必須 MCP の宣言・厳格検証は未定義（MVP-6）。仕事の自動分割と優先順位付けは未実装で、単一リポジトリ・単一フロー単位に割り切っている |
| **④ 調査・実装・修正** | 許可範囲内で再現、調査、実装、修正を行い、仕様判断が必要なら人へ問いを返す | `codex` / `claude` / `antigravity` / `custom` / `mock` の各アダプターと事後ガード（`guard.py`）を実装済み | 結合テストとモック検証は完了済みだが、実対象リポジトリでの実エージェントによる `fix_and_retry` 本番運用は MVP-7 で扱う |
| **⑤ 検証** | 必須チェック、実挙動シナリオ、形式検証を同じ版で実行し、2 層判定と構造化証拠を残す | `check` / `formal_check` の 2 層判定、`required_evidence` / `formal.json` 検証、ナーサリー自身の有界全状態探索を実装済み | 対象リポジトリ側の形式検証適用は MVP-8 で扱う。複数リポジトリをまたぐ結合シナリオも未対応 |
| **⑥ 反映・再修正・人への引き渡し** | 委譲範囲内の自動反映、再修正、証拠付きの結実通知、例外のエスカレーションを自律的に振り分ける | `garden report`（要約、HTML マーカー、ラベル自動作成、Artifacts URL）と `garden reflect`（祖先および差分検証付き PR 作成・自動マージゲート）を実装済み | 実対象リポジトリは安全な `observe_only` で運用中。`propose_pr` / `auto_merge` 実運用は MVP-7、結実後のリリース・デプロイ連携は MVP-8 で扱う |
| **⑦ 工程そのものの改善** | 介入記録とメトリクスから再発防止を検証し、有効な知見を共通テンプレートへ還元する | `garden feedback`（CLI / `/garden feedback` コメント解析・PR 永続化）、`garden metrics`、`garden upgrade`、`garden template-gate` を実装済み | 介入から対象固有のハーネス改善までの 1 周は HAB で実証済み。複数ガーデンから共通テンプレート `v2` への還元実績は MVP-8 で扱う |

---

## 3. 現在の実装構成と検証コマンド

Python 3.11 以上・標準ライブラリのみで動作する。モジュール構成の詳細は [詳細設計書 §1.2](../design/detailed-design.md#12-ナーサリーのディレクトリ構成)、全サブコマンド仕様は [詳細設計書 §8](../design/detailed-design.md#8-cli-リファレンスgarden-サブコマンド一覧) を参照する。

- **テンプレート（4 型 × v1）**: `templates/{cli,web-frontend,http-api,library}/v1/`（各型に `template.toml`、`garden.toml.tmpl`、`spec.md.tmpl`、`workflow.yml.tmpl`、`release-evidence.json` を同梱）
- **共通の開発ハーネスマニフェスト**: `harnesses/pstack.toml`（`pinned_ref` 管理と `doctor` の探索パス定義のみ。個別スキル・MCP の必須検査は未実装）
- **GitHub Actions ワークフロー（4 本）**:
  - `.github/workflows/garden-reusable.yml`（対象リポジトリから呼ぶ汎用検証・報告ワークフロー）
  - `.github/workflows/garden-feedback-reusable.yml`（`/garden feedback` コメントからの介入記録ワークフロー）
  - `.github/workflows/ci.yml`（ナーサリー単体・結合テスト CI）
  - `.github/workflows/garden-dogfood.yml`（ナーサリー自己ドッグフーディングと `reusable-smoke`）
- **現在の検証コマンド**（最新の件数と合否は以下で再確認できる）:
  ```sh
  python3 -m unittest test_garden.py -v
  python3 -m unittest discover -s tests -v
  python3 .agents/skills/formal-verify-garden-engine/scripts/verify_invariants.py
  ```

---

## 4. 現在の割り切りと既知の制約

MVP-4 時点で意図的に割り切っている点と既知の制約をまとめる。各制約の解消手順と完了条件は [MVP 成長プラン](roadmap.md#4-今後の成長フェーズmvp-5mvp-8-と最終形) に記載している。

1. **初回リリースタグ未発行（対応予定: MVP-5）**
   `garden init --nursery-ref <ref>` と再利用ワークフローの `nursery_ref` はブランチ・タグ・コミット SHA に対応しているが、ナーサリー側の初回安定タグ（`v0.1.0`）は未発行である。
2. **報告・ラベル遷移の運用エッジケース（対応予定: MVP-5）**
   新ラベル追加失敗時の旧ラベル保護、同一 PR 並行実行時の `concurrency.group` 競合防止、過去実行再表示時の `artifacts_url` 混入防止、既存ラベル色・説明の上書き防止が未対応である。
3. **必須スキル・必須 MCP の未定義と pstack 手動導入（対応予定: MVP-6）**
   MCP サーバーの宣言・検査は未実装であり、スキルも `harnesses/pstack.toml` の推奨定義にとどまる。`garden doctor` は pstack ディレクトリ未検出時も `warn` で通し、自動導入導線やエージェントプラグイン連携も未整備である。
4. **実対象リポジトリの既定モードは `observe_only`（対応予定: MVP-7）**
   全テンプレートと HAB の既定委譲モードを `observe_only` に固定している。実エージェントによる修正と `garden reflect` の PR 提案はハーネス標準化後に解放する。
   MVP-4 の事後ガードは、書き込み可能なモードで `allowed_paths` が空の場合に許可パスの照合を省く。書き込み範囲を絞るには非空のリストを指定する。フローで既定のリストを継承するときは `allowed_paths` を省略し、変更を禁止するときは `observe_only` を使う。
5. **非公開ナーサリー利用時のトークン要件（運用制約）**
   非公開ナーサリーから別の非公開リポジトリの再利用ワークフローを実行する場合は、ナーサリー側の Actions アクセス許可と、呼び出し側での `secrets.nursery_token`（Contents 読み取り権限）設定が必要である。
6. **単一リポジトリ評価への割り切り（対応予定: MVP-5 以降）**
   `garden run` と `garden check` は各対象リポジトリ単体の作業ツリーを評価単位としており、複数リポジトリをまたぐ結合シナリオは未対応である。
7. **1 号目 HAB の対象固有のハーネス改善候補（対応予定: MVP-5）**
   HAB のデモスクリプトは失敗時に構造化 JSON を出力しないため、`UNSATISFIED` の要約から失敗段階まで直接読み取れない（詳細: [HAB 実証記録](evidence/mvp4-hab/README.md#今後の改善候補post-mvp)）。

---

## 5. 実証記録

- [HAB での MVP-4 実証記録](evidence/mvp4-hab/README.md)（1 号目の実対象リポジトリでのローカル実証と GitHub Actions 稼働記録）

---

[リポジトリ概要](../../README.md) · [MVP 成長プラン](roadmap.md) · [概要設計書](../design/overview.md) · [詳細設計書](../design/detailed-design.md) · [使い方](../guides/usage.md) · [用語集](../../GLOSSARY.md)
