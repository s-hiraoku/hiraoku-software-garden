# MVP-4 の実証記録: 1号目の対象リポジトリ HAB

[詳細設計書 §9 MVP-4](../../detailed-design.md#mvp-4-1号目の実対象リポジトリ接続設計書セクション-6-の完了判定) の完了判定として、[設計書 §6](../../design.md#6-最初の到達点と完了判定) の 3 条件を、実際の対象リポジトリ [s-hiraoku/hiraoku-agent-base](https://github.com/s-hiraoku/hiraoku-agent-base)（HAB）で確かめた記録です。

## 対象リポジトリに入れたもの

HAB 側のブランチ `claude/project-thread-xq4x92` に、`templates/cli/v1` から作ったガーデンを入れています。

| ファイル | 内容 |
| --- | --- |
| `.garden/garden.toml` | ガーデン定義。フロー `verify_relay`（`toolchain` → `relay_e2e` → ゲート → 報告）。委任は `observe_only` |
| `docs/specs/message-relay.md` | 承認済み仕様。AC-001（ツールチェーンと依存）、AC-002（ローカルMVPの13段階と4配送のACK）、INV-001・INV-002 |
| `.garden/harness/check-toolchain.mjs` | 対象固有のハーネス。環境不備は終了コード 75（INCONCLUSIVE） |
| `.garden/harness/verify-relay.mjs` | 対象固有のハーネス。`node scripts/hab-mvp-demo.mjs --simulate` の結果を証拠 `hab-mvp-result.json` に残し、挙動不一致は 1、環境不備は 75 |
| `.garden/interventions/20261123T114220Z-a75b.json` | 条件3で記録した介入レコード |
| `.github/workflows/garden.yml` | ナーサリーの再利用ワークフローを呼ぶ。`node_version_file: .node-version` |

## 実行の記録

2026-11-23 に、HAB ブランチの複製をローカルの `main`（承認ブランチ）に見立て、`garden run` で実行しました。各ディレクトリに `summary.md` と `run.json` を残しています。

| 条件 | 実行 | 状況 | 判定 |
| --- | --- | --- | --- |
| 1. 普段の確認を任せられる | [20261123T114125Z-78dcbfbc](20261123T114125Z-78dcbfbc/summary.md) | `pull_request` / `opened` を `[[triggers]]` から `verify_relay` に解決。Node 26.13.1 | `SATISFIED`（AC・INV すべて satisfied） |
| 2. 挙動不一致を区別できる | [20261123T114207Z-d8063ca1](20261123T114207Z-d8063ca1/summary.md) | `SqliteDelivery` が ACK receipt を記録しない回帰を注入（実証用で、HAB には入れていない） | `UNSATISFIED`（AC-001 は satisfied、AC-002 は unsatisfied） |
| 2. 環境不備を区別できる | [20261123T114214Z-beaf6ea5](20261123T114214Z-beaf6ea5/summary.md) | Node 22.23.5 で実行 | `INCONCLUSIVE`（AC-001 は inconclusive、AC-002 以降は not_evaluated） |
| 3. 人の手直しが次に効く | [20261123T114237Z-a72b7edf](20261123T114237Z-a72b7edf/summary.md) | 介入を記録し、ハーネスを v2 にしてから同じ環境不備を再実行 | `INCONCLUSIVE`、原因を [toolchain-diagnostic.json](20261123T114237Z-a72b7edf/evidence/toolchain-diagnostic.json) に記録 |
| （確認） | [20261123T114242Z-a72b7edf](20261123T114242Z-a72b7edf/summary.md) | v2 ハーネスを Node 26.13.1 で実行 | `SATISFIED` |

### 条件3の流れ

1. v1 ハーネスでは、要約の備考が「exited with inconclusive code 75」だけで、原因（必要な Node 26.13.1 と実際の 22.23.5 の違い）は [stderr.log](20261123T114214Z-beaf6ea5/toolchain-stderr.log) を開かないと分かりませんでした。
2. `garden feedback from-comment` で介入を記録しました（[20261123T114220Z-a75b.json](20261123T114220Z-a75b.json)、分類 `harness_bug`）。
3. 再発防止として、`check-toolchain.mjs` が必要な版・実際の版・対処を `toolchain-diagnostic.json` に証拠として残すようにし、対象固有のハーネスを v2 にしました。
4. 同じ環境不備で再実行すると、ガーデンが原因を証拠付きで報告しました。介入を `verified` に更新し、`garden metrics` で `verified_rate = 1.0` を確認しました。

## まだ確かめていないこと

- **GitHub Actions からの起動**: 今回はすべてローカル実行です。HAB の PR を起点に CI で動かすには、HAB の PR を `main` にマージして仕様を承認し、HAB（公開）からナーサリー（非公開）の再利用ワークフローを呼べるようにする必要があります。
- **失敗した段階の特定**: HAB のデモは失敗時に JSON を出さないため、`UNSATISFIED` の要約からはどの段階で失敗したかまでは分かりません。次の介入の候補です。
