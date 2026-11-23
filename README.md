# hiraoku-software-garden

ソフトウェアを植物として捉え、それぞれのリポジトリに育成環境を整えるための開発の拠点（ナーサリー）です。ここに蓄えた育て方のテンプレートを、対象リポジトリの用途と仕様に合わせて適用すると、そのソフトウェアのガーデンが生まれます。

## 目指すソフトウェアファクトリー

1. **人が仕様を定める。** 誰のどんな願いをかなえるのか、守るべき性質と完成の条件を決めます。
2. **ガーデンが育てる。** トリガーを受けてAIが実装と検証を繰り返します。仕様を満たしたと判断したら、成果物と検証の根拠を添えて人に知らせます。
3. **人が完成を判断する。** 成果物を確かめ、出荷するか、さらに手を入れるかを決めます。

仕様の役割、形式検証を取り入れる理由、収穫の判断についての基本方針をまとめています。[ソフトウェアガーデンの思想](docs/philosophy.md)

## このリポジトリの役割

複数の対象リポジトリで使う開発手順と、対象ごとのチェックを呼び出す入口を、このナーサリーに置きます。共通の開発ハーネスには pstack を使い、製品固有の仕様、設計上の制約、検証の実装は対象リポジトリで育てます。一つのガーデンで得た知見が他のガーデンでも役立つと確かめられたら、共通のテンプレートに取り込みます。

## 必要なもの

- Python 3.11 以上（標準ライブラリのみで動き、追加の依存パッケージはありません）
- Git
- GitHub CLI `gh`（任意。`garden doctor` のブランチ保護の確認と、`garden report` の Issue / PR への投稿に使います）
- 共通の開発ハーネス pstack（任意。`garden doctor` が導入済みかを確認します）

## ナーサリーを準備する

ナーサリーをクローンします。対象リポジトリと同じ親ディレクトリに置くと、カタログのパスを短く書けます。

```sh
git clone https://github.com/s-hiraoku/hiraoku-software-garden.git
cd hiraoku-software-garden
python3 garden.py --help
```

`garden` コマンドとして使う場合は、仮想環境（`venv` など）内で編集可能モードでインストールします。以降の例は `garden` と書きますが、`python3 garden.py` に読み替えても同じように動きます。

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
garden --help
```

## 対象リポジトリにガーデンを導入する

### 1. ガーデンの雛形を生成する

対象リポジトリのルートで `garden init` を実行します。ファイル構成から型を自動で判定し、テンプレートからガーデン定義と仕様の雛形を生成します。現在使えるテンプレートは `cli`、`web-frontend`、`http-api`、`library` の 4 種類です。判定できない場合や明示したい場合は `--type <型名>` で指定します。

```sh
garden init --target path/to/your-repo
```

生成されるファイルは次のとおりです。既存のファイルは `--force` を付けない限り上書きしません。

| ファイル | 役割 |
| --- | --- |
| `.garden/garden.toml` | ガーデン定義。チェック、トリガー、フロー、委譲範囲を書く |
| `.garden/.gitignore` | 実行記録（`runs/`）と作業用ワークツリー（`worktrees/`）をコミット対象から外す |
| `docs/specs/<リポジトリ名>.md` | 仕様の雛形（`status: draft`） |
| `.github/workflows/garden.yml` | PR ごとにガーデンを動かす GitHub Actions ワークフロー |

### 2. 仕様とチェックを整える

1. `docs/specs/<リポジトリ名>.md` に目的、受け入れ条件（`AC-xxx`）、不変条件（`INV-xxx`）を書きます。
2. `.garden/garden.toml` の初期チェック（例: `[checks.smoke]`）にある仮のコマンドを、対象リポジトリの実際のテストやチェックに置き換えます。`command` は引数の配列で書きます。`covers` には、そのチェックが確かめる条件 ID を並べます。
3. 仕様のすべての条件を、いずれかのチェックの `covers` で覆います。チェックのない条件は「確認できなかった」と扱われ、判定が `INCONCLUSIVE` になります（雛形の初期状態では `INV-001` に対応するチェックが未設定のため、必要に応じて追加するか `covers` を合わせます）。
4. 仕様のフロントマターを `status: approved` にして、承認ブランチ（既定は `main`）にマージします。

ガーデンは、CLI / ワークフローで指定された信頼できる承認ブランチ（既定 `main`、変更時は `--approval-branch`）にマージされた仕様とガーデン定義だけを評価の基準にします。作業中のブランチで仕様や定義を書き換えても判定の基準は変わらず、書き換えたことが実行記録に残ります。スキーマの全体は[詳細設計書の「ガーデン定義スキーマ」](docs/implementation/detailed-design.md#2-ガーデン定義スキーマgardengardentoml)を参照してください。

### 3. 診断する

```sh
garden doctor --target path/to/your-repo
```

Git の状態、ガーデン定義の妥当性、承認済み仕様の有無、チェックコマンドを起動できるか、pstack の導入状況、`.garden/.gitignore`、承認ブランチの保護を確認します。`error` が一つでもあると終了コード 1 を返します。ネットワークを使わずに確かめるときは `--skip-network`、機械で読むときは `--json` を付けます。

### 4. 手元でフローを実行する

```sh
garden run verify_only --target path/to/your-repo
garden report path/to/your-repo/.garden/runs/<run_id>
```

`garden run` はガーデン定義のフローを実行し、判定と実行記録の場所を JSON で出力します。フロー名に `auto` を渡すと、`--trigger-event` と `--trigger-action` に一致する `[[triggers]]` からフローを自動解決します。実行記録は `.garden/runs/<run_id>/` に残り、`garden report` で人が読める要約（Markdown または `--format json`）にできます。同じコミット・同じ承認済み仕様・同じ定義ですでに `SATISFIED` になっている場合は再実行を省きます（CI では `--check-github-markers` により Issue / PR の HTML マーカーも照合します）。やり直すときは `--force` を付けます。
また、`fix_and_retry` を含むフローでは `[agents.<名前>]` に `codex` / `claude` / `antigravity` / `custom` / `mock` の各アダプターを指定でき、エージェントの応答を `response.json` へ正規化したうえで事後ガードで変更範囲を検証します。

| 判定 | 終了コード | 意味 |
| --- | --- | --- |
| `SATISFIED` | 0 | すべての条件をチェックで確かめられた |
| `UNSATISFIED` | 1 | 満たしていない条件がある |
| `INCONCLUSIVE` | 3 | 確かめられなかった条件がある（チェックがない、結果を判断できないなど） |
| `NEEDS_HUMAN` | 4 | 人の判断が必要になった（仕様の判断を求められた、エージェントの変更が委譲範囲を外れた、時間の上限を超えたなど） |
| `ABORTED` | 5 | `on_fail = "abort"` などの指定により、フローを途中で打ち切った |

CLI の引数やガーデン定義に誤りがある場合は、終了コード 2 を返します。

### 5. GitHub Actions で動かす

生成された `.github/workflows/garden.yml` をコミットすると、ナーサリーの再利用ワークフロー（[garden-reusable.yml](.github/workflows/garden-reusable.yml)）が呼ばれます。このワークフローは対象チェックアウト外の `$RUNNER_TEMP` にナーサリーの CLI を取得して `garden run` を実行し、実行記録をアーティファクトとして保存して、結果を PR にコメントします。

| 起動のきっかけ | 実行するフロー |
| --- | --- |
| PR の作成・更新 | `auto`（`.garden/garden.toml` の `[[triggers]]` から解決。初期雛形は `verify_only`） |
| Issue / PR に `/garden resume` とコメント | `verify_only`（PR の場合は `refs/pull/<番号>/head` をチェックアウトして実行） |
| 手動実行（workflow_dispatch） | 入力で指定したフロー |

判定が `SATISFIED` 以外のときはジョブが失敗します。承認ブランチを保護し、このジョブを必須のチェックにすると、確かめられていない変更をマージできなくなります。

### 6. ナーサリーのカタログに登録する（任意）

ナーサリーの [garden.toml](garden.toml)（カタログ）に対象リポジトリを登録すると、ナーサリーから複数の対象リポジトリのチェックをまとめて実行できます。

```toml
[repositories.my-cli]
path = "my-cli"                                   # --workspace（既定はナーサリーの親ディレクトリ）からの相対パス、または絶対パス
remote = "https://github.com/you/my-cli.git"      # 対象のチェックアウトで git remote get-url が返す URL
description = "メッセージ送受信を確認する CLI"
check = ["python3", "-m", "unittest"]
```

```sh
garden list
garden check my-cli
```

詳しくは[ナーサリーの設計と使い方](docs/implementation/README.md#リポジトリを追加する)を参照してください。

## その他のコマンド

| コマンド | 用途 |
| --- | --- |
| `garden upgrade --target <path> [--to <版>] [--apply]` | テンプレートの新しい版との差分を示し、`--apply` で独自の設定を保ったまま更新する |
| `garden feedback add --target <path> --run-id <id> --category <分類> --summary <内容>` | 人が介入した内容を記録する |
| `garden feedback summary --target <path>` | 記録した介入を集計する |

全サブコマンドの仕様は[詳細設計書の CLI リファレンス](docs/implementation/detailed-design.md#8-cli-リファレンスgarden-サブコマンド一覧)にあります。

## ナーサリー自体を開発する

```sh
python3 -m unittest test_garden.py -v
python3 -m unittest discover -s tests -v
python3 .agents/skills/formal-verify-garden-engine/scripts/verify_invariants.py
```

PR と `main` への push では、[Nursery CI](.github/workflows/ci.yml) が同じテストを実行します。

## 現在の制約と今後決めること

- 対象リポジトリのワークフローは、ナーサリーの `main` を直接参照します。版を固定する配布方法（タグやリリース）はまだ決めていません。
- pstack の導入手順はまだ用意していません。`garden doctor` は [harnesses/pstack.toml](harnesses/pstack.toml) の探索パスに pstack があるかを確認するだけです。
- ナーサリーを非公開にする場合、対象リポジトリの GitHub Actions からナーサリーを読み取る権限の設定が必要です。
- 実際の対象リポジトリへの導入（MVP-4）と、実 GitHub Actions 環境での `garden-reusable.yml` の実稼働確認はまだ行っていません。

## 文書案内

- 仕様の承認、実行と検証、テンプレートの運用方針をまとめています。[ソフトウェアガーデン設計書](docs/implementation/design.md)
- アーキテクチャ、`.garden/garden.toml` スキーマ、ステートマシン、プロトコル、CLI 仕様をまとめています。[ソフトウェアガーデン詳細設計書](docs/implementation/detailed-design.md)
- 検証環境を整え、再発する問題をチェックに変え、信頼できる手順から自動化を広げます。[開発と検証の進め方](docs/implementation/README.md)
- 背景にある実践や共有資料を参照できます。[ドキュメント案内](docs/README.md)
