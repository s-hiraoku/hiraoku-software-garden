# ナーサリーとガーデンの使い方

この文書では、対象リポジトリにガーデンを導入して運用する手順、ナーサリーのカタログで複数リポジトリを管理する方法、およびナーサリー自身を開発して検証する手順をまとめます。

設計の全体像は [ソフトウェアガーデン概要設計書](../design/overview.md)、技術仕様と全 CLI オプションは [ソフトウェアガーデン詳細設計書](../design/detailed-design.md)、現在の実装範囲と制約は [現在の MVP 設計](../evolution/current-mvp.md) を参照してください。

---

## 1. 必要なもの

- [uv](https://docs.astral.sh/uv/)（推奨）または [pipx](https://pipx.pypa.io/)。ナーサリーの CLI をインストールせずにその場で実行するために使います。
- Python 3.11 以上（CLI は標準ライブラリだけで動きます）。
- Git。
- GitHub CLI `gh`（任意。`garden doctor` のブランチ保護の確認、`garden report` の Issue / PR 投稿、`garden reflect` の PR 作成と自動マージに使います）。
- 共通の開発ハーネス pstack（任意。`garden doctor` が導入状況を確認します）。

---

## 2. CLI を実行する

ナーサリーをクローンしたりインストールしたりする必要はありません。対象リポジトリのルートで、次の 1 行を実行します。

```sh
uvx --from git+https://github.com/s-hiraoku/hiraoku-software-garden garden init --target .
```

uv がない環境では pipx を使います。

```sh
pipx run --spec git+https://github.com/s-hiraoku/hiraoku-software-garden garden init --target .
```

どちらも GitHub からナーサリーを取得し、一時的な環境で `garden` コマンドを実行します。テンプレートもパッケージに含まれているため、`init` から `doctor`、`run` まで同じ方法で使えます。以降の例では、次のエイリアスを設定したものとして `garden` と省略して書きます。

```sh
alias garden='uvx --from git+https://github.com/s-hiraoku/hiraoku-software-garden garden'
```

---

## 3. 対象リポジトリにガーデンを導入する

### 手順 1: ガーデンの雛形を生成する

対象リポジトリに対して `garden init` を実行します。ファイル構成から型を自動で判定し、テンプレートからガーデン定義と仕様の雛形を生成します。現在使えるテンプレートは `cli`、`web-frontend`、`http-api`、`library` の 4 種類です。判定できない場合や明示したい場合は `--type <型名>` を指定します。

```sh
garden init --target path/to/your-repo
# 実在するコミット SHA やタグでナーサリーの版を固定する場合:
garden init --target path/to/your-repo --nursery-ref <commit-sha-or-tag>
```

`--nursery-ref` には、ナーサリーリポジトリに実在するブランチ名、タグ、またはコミット SHA を指定してください。未発行のタグを指定すると、生成された GitHub Actions ワークフローが参照解決に失敗します。

生成されるファイルは次のとおりです。既存のファイルは `--force` を付けない限り上書きしません。

| ファイル | 役割 |
| --- | --- |
| `.garden/garden.toml` | ガーデン定義。チェック、トリガー、フロー、委譲範囲を書く |
| `.garden/.gitignore` | 実行記録（`runs/`）と作業用ワークツリー（`worktrees/`）をコミット対象から外す |
| `docs/specs/<リポジトリ名>.md` | 仕様の雛形（`status: draft`） |
| `.github/workflows/garden.yml` | PR ごとにガーデンを動かす GitHub Actions ワークフロー |

### 手順 2: 仕様とチェックを整える

1. `docs/specs/<リポジトリ名>.md` に目的、受け入れ条件（`AC-xxx`）、不変条件（`INV-xxx`）を書きます。
2. `.garden/garden.toml` の初期チェック（例: `[checks.smoke]`）にある仮のコマンドを、対象リポジトリの実際のテストやチェックに置き換えます。`command` は引数の配列で書き、`covers` にはそのチェックが確かめる条件 ID を並べます。
3. 仕様のすべての条件を、いずれかのチェックの `covers` で覆います。チェックのない条件は「確認できなかった」と扱われ、判定が `INCONCLUSIVE` になります（雛形の初期状態では `INV-001` に対応するチェックが未設定のため、必要に応じて追加するか `covers` を合わせます）。
4. 仕様のフロントマターを `status: approved` にして、承認ブランチ（既定は `main`）にマージします。

ガーデンは、CLI またはワークフローで指定された信頼できる承認ブランチ（既定 `main`、変更時は `--approval-branch`）にマージされた仕様とガーデン定義だけを評価の基準（Trust Anchor）にします。作業中のブランチで仕様や定義を書き換えても判定の基準は変わらず、書き換えたことが実行記録に残ります。スキーマの詳細は [詳細設計書 §2](../design/detailed-design.md#2-ガーデン定義スキーマgardengardentoml) を参照してください。

### 手順 3: 診断する

```sh
garden doctor --target path/to/your-repo
```

Git の状態、ガーデン定義の妥当性、承認済み仕様の有無、チェックコマンドを起動できるか、pstack の導入状況、`.garden/.gitignore`、承認ブランチの保護を確認します。`error` が一つでもあると終了コード `1`、Git 異常や定義読込不可では終了コード `2` を返します。ネットワークを使わずに確かめるときは `--skip-network`、機械で読むときは `--json` を付けます。

### 手順 4: 手元でフローを実行する

```sh
garden run verify_only --target path/to/your-repo
garden report path/to/your-repo/.garden/runs/<run_id>
```

`garden run` はガーデン定義のフローを実行し、判定と実行記録の場所を JSON で出力します。フロー名に `auto` を渡すと、`--trigger-event` と `--trigger-action` に一致する `[[triggers]]` からフローを自動解決します。

- 実行記録は `.garden/runs/<run_id>/` に残り、`garden report` で人が読める要約（Markdown または `--format json`）にできます。
- 同じコミット、同じ承認済み仕様、同じ定義ですでに `SATISFIED` になっている場合は再実行を省きます（CI では `--check-github-markers` により Issue / PR の HTML マーカーも照合します）。やり直すときは `--force` を付けます。
- `fix_and_retry` を含むフローでは `[agents.<名前>]` に `codex` / `claude` / `antigravity` / `custom` / `mock` の各アダプターを指定でき、エージェントの応答を `response.json` へ正規化したうえで事後ガードで変更範囲を検証します。

| 判定 | 終了コード | 意味 |
| --- | --- | --- |
| `SATISFIED` | `0` | すべての条件をチェックで確かめられた |
| `UNSATISFIED` | `1` | 満たしていない条件がある |
| `INCONCLUSIVE` | `3` | 確かめられなかった条件がある（チェックがない、環境不備、結果を判断できないなど） |
| `NEEDS_HUMAN` | `4` | 人の判断が必要になった（仕様未承認、仕様判断の問い、委譲範囲外の変更、時間・コスト上限超過など） |
| `ABORTED` | `5` | `on_fail = "abort"` などの指定により、フローを途中で打ち切った |

CLI の引数やガーデン定義に誤りがある場合は、終了コード `2` を返します。

### 手順 5: GitHub Actions で動かす

生成された `.github/workflows/garden.yml` をコミットすると、ナーサリーの再利用ワークフロー（[garden-reusable.yml](../../.github/workflows/garden-reusable.yml)）が呼ばれます。このワークフローは対象チェックアウト外の `$RUNNER_TEMP` にナーサリーの CLI を取得して `garden run` を実行し、実行記録をアーティファクトとして保存して、結果を PR にコメントします。

| 起動のきっかけ | 実行するフローと条件 |
| --- | --- |
| PR の作成・更新 | `auto`（`.garden/garden.toml` の `[[triggers]]` から解決。初期雛形は `verify_only`） |
| PR に `/garden resume` とコメント | `verify_only`（PR の `refs/pull/<番号>/head` をチェックアウトして実行。コメント投稿者が `OWNER`、`MEMBER`、`COLLABORATOR` のいずれかである必要がある。非 PR の Issue では失敗する） |
| Issue または PR に `/garden feedback <分類> <内容> --run-id <実行ID>` とコメント | [garden-feedback-reusable.yml](../../.github/workflows/garden-feedback-reusable.yml) で介入レコードを記録（`OWNER`、`MEMBER`、`COLLABORATOR` のみ実行可。`open_pr: true` で PR 永続化も可能） |
| 手動実行（`workflow_dispatch`） | 入力で指定したフロー |

判定が `SATISFIED` 以外のときはジョブが失敗します。承認ブランチを保護し、このジョブを必須のチェックにすると、確かめられていない変更をマージできなくなります。

---

## 4. 共通の開発ハーネスと対象固有のハーネスを整える

共通の開発ハーネスと対象固有のハーネスの責務分担は [用語集（ユビキタス言語）](../../GLOSSARY.md) と [概要設計書 §4](../design/overview.md#4-ナーサリー対象リポジトリ実行環境の責務) を参照してください。導入時は次の手順で環境を整えます。

1. **共通の開発ハーネス（pstack）を配置する。** [harnesses/pstack.toml](../../harnesses/pstack.toml) の `upstream` と `pinned_ref` に従い、`~/.gemini/config/plugins/pstack`、`~/.cursor/plugins/pstack`、または対象リポジトリの `.agents/skills` に pstack を配置します。
2. **対象固有のハーネスを対象リポジトリへ実装する。** 製品の起動、操作、期待結果の確認、および環境不備（終了コード `75`）と挙動不一致（終了コード `1`）の切り分けを行うチェックコマンドを対象リポジトリに置きます。
3. **`.garden/garden.toml` からチェックを結びつける。** `[checks.<名前>]` にコマンド配列と `covers`、`required_evidence` を設定し、`garden doctor` で起動確認と pstack 検出を確かめます。
4. **再発した失敗をチェックへ取り込む。** 手直しが必要になった場合は `garden feedback` で介入を記録し、対象固有のハーネスへ再発防止チェックを追加してから再実行で効果を確認します。

---

## 5. ナーサリーのカタログで複数リポジトリを管理する

ナーサリーの [garden.toml](../../garden.toml)（カタログ）に対象リポジトリを登録すると、ナーサリーから複数の対象リポジトリのチェックや健全性をまとめて確認できます。カタログはナーサリーのチェックアウトにあるため、この操作はナーサリーをクローンして行います。

### リポジトリを登録する

`garden register` を使うか、[garden.toml](../../garden.toml) に直接追記します。`--check` を省略できるのは対象リポジトリに `.garden/garden.toml` が既にある場合だけで、その場合は `garden doctor --target . --skip-network` が自動登録されます。任意のテストコマンドを登録する場合は `--check` で明示します。

```sh
python3 garden.py register path/to/my-cli \
  --description "メッセージ送受信を確認する CLI" \
  --check python3 -m unittest
```

上記コマンドで `garden.toml` に追加されるエントリ形式は次のとおりです。

```toml
[repositories.my-cli]
path = "my-cli"                                   # --workspace（既定はナーサリーの親ディレクトリ）からの相対パス、または絶対パス
remote = "https://github.com/you/my-cli.git"      # 対象のチェックアウトで git remote get-url が返す取得 URL
description = "メッセージ送受信を確認する CLI"
check = ["python3", "-m", "unittest"]
```

### 一覧・チェック・横断監視

```sh
python3 garden.py list
python3 garden.py check my-cli
python3 garden.py status --skip-network
```

別のワークスペースやカタログファイルを使うときは、サブコマンドの前に `--workspace` と `--catalog` を付けます。

```sh
python3 garden.py --workspace "$PWD/../projects" --catalog my-garden.toml check my-repo
```

`check` の標準出力は JSON で、対象、実行コマンド、開始前のコミットと変更の有無、結果を記録します。

| 結果 | 意味 |
| --- | --- |
| `passed` | チェックコマンドの終了コードが `0` |
| `failed` | チェックコマンドが非ゼロで終了。原因は標準エラーのログを確認する |
| `unavailable` | チェックアウトが見つからない、取得 URL が一致しない、Git 情報を読めない、コマンドを起動できないなどの理由で実行できなかった |

全件成功で終了コード `0`、失敗または実行不可があれば `1`、未登録の対象や引数・カタログ不正は `2` を返します。

---

## 6. 保守・反映・測定コマンド

| コマンド | 用途 |
| --- | --- |
| `garden upgrade --target <path> [--to <版>] [--apply]` | テンプレートの新しい版との差分を示し、`--apply` で独自の設定を保ったまま更新する |
| `garden spec-diff --target <path> [--base main] [--head REF]` | 承認済み仕様との条件単位差分と影響チェック・フローを解析する |
| `garden template-gate (--type TYPE \| --all) [--version N] [--output PATH]` | テンプレート公開ゲートを実行し `release-evidence.json` を生成する（`--type` または `--all` のどちらか必須） |
| `garden reflect <run-dir> [--base main] [--execute]` | 工程⑥の反映ゲートを評価する（既定はドライラン） |
| `garden feedback add\|from-comment\|update\|summary --target <path>` | 人の介入レコードの記録、状態更新、集計を行う |
| `garden metrics --target <path> [--limit N]` | 実行記録と介入レコードから判定率、再発条件、介入解消率を集計する |

全サブコマンドの仕様と終了コードは [詳細設計書 §8 CLI リファレンス](../design/detailed-design.md#8-cli-リファレンスgarden-サブコマンド一覧) を参照してください。

---

## 7. ナーサリー自体を開発・検証する

ナーサリーを変更するときや、カタログを使うときはクローンします。`python3 garden.py`（または `python3 -m garden`）で直接実行するか、仮想環境内で編集可能モードでインストールして `garden` コマンドとして使います。

```sh
git clone https://github.com/s-hiraoku/hiraoku-software-garden.git
cd hiraoku-software-garden
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
```

テストと形式検証のコマンドは次のとおりです。

```sh
python3 -m unittest test_garden.py -v
python3 -m unittest discover -s tests -v
python3 .agents/skills/formal-verify-garden-engine/scripts/verify_invariants.py
```

CI ワークフローの発火条件は次のとおりです。

- [Nursery CI](../../.github/workflows/ci.yml) は、すべての PR と `main` への push で実行されます。
- [Garden Self-Dogfood](../../.github/workflows/garden-dogfood.yml) は、`main` への push、手動実行（`workflow_dispatch`）、および `.garden/**`、`docs/specs/**`、対象ワークフロー、形式検証スキルを変更する PR で実行されます。

---

[リポジトリ概要](../../README.md) · [概要設計書](../design/overview.md) · [詳細設計書](../design/detailed-design.md) · [現在の MVP 設計](../evolution/current-mvp.md) · [MVP 成長プラン](../evolution/roadmap.md) · [用語集](../../GLOSSARY.md)
