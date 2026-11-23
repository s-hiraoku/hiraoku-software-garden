# 用語集

人とエージェントがこのリポジトリについて話すときに、同じ言葉を同じ意味で使うための用語集です。hiraoku-software-garden が独自に定義する用語だけを載せます。外部の資料やツールで定義された用語は、それぞれの出典を参照します。定義はリポジトリ内の記述に沿っています。「使い分け・注意」列には、混同しやすい言葉と避けたい言い方を書いています。用語の意味を変えたり新しい用語を導入したりしたときは、この表も更新します。

## 中心概念

いちばん混同しやすいのは「Garden」と「ガーデン」です。前者はこのリポジトリ、後者は対象リポジトリごとに生まれる育成環境を指します。

| 用語 | 定義 | 使い分け・注意 | 出典 |
| --- | --- | --- | --- |
| Garden（Home Repo） | このリポジトリ hiraoku-software-garden そのもの。複数プロジェクト共通の開発手順と、対象ごとのチェックを呼び出す入口を置く拠点。 | 英字の「Garden」はこのリポジトリを指す。個々の対象の育成環境は「ガーデン」と呼び分ける。 | [README.md](README.md), [AGENTS.md](AGENTS.md) |
| ガーデン（庭） | Garden のテンプレートを対象リポジトリの用途と仕様に合わせて適用したときに生まれる、そのソフトウェアの育成環境。 | 「庭」は同義。Garden（このリポジトリ）と混ぜない。「一つの庭」と言えば対象リポジトリ一つ分。 | [docs/philosophy.md](docs/philosophy.md) |
| 対象リポジトリ | Garden に登録して育てる個々のプロダクトのリポジトリ。プロダクトコード、固有の仕様、ハーネスの実装と依存関係を持つ。 | 「プロジェクト」とほぼ同義だが、ファイルの置き場を論じるときは「対象リポジトリ」を使う。 | [docs/implementation/README.md](docs/implementation/README.md) |
| ソフトウェアファクトリー | 人が仕様を定め、ガーデンが実装と検証を繰り返し、人が完成を判断する、という開発の仕組み全体。このプロジェクトが目指す姿。 | 外部事例（Pocock、StrongDM など）の意味とは少しずつ異なる。外部の意味を指すときは「〇〇の言うソフトウェアファクトリー」と明示する。 | [README.md](README.md), [docs/philosophy.md](docs/philosophy.md) |
| テンプレート（育て方） | 種類に合う手順、道具、設計上の制約の組み合わせとして Garden に蓄える、繰り返し使える育て方の知識。 | ファイルの雛形の意味ではない。現時点では文書上の方針で、実体のテンプレートファイルはまだない。 | [docs/philosophy.md](docs/philosophy.md) |
| ハーネス | エージェントが設計上の制約を守り、実装した挙動を自分で確かめられるようにする仕組み。 | 「共通の開発ハーネス」と「プロジェクト固有のハーネス」のどちらかを必ず明示する。 | [docs/implementation/README.md](docs/implementation/README.md) |
| 共通の開発ハーネス | 全プロジェクトで使う開発の進め方。現在は pstack を採用。 | Garden 側の責務。 | [docs/implementation/README.md](docs/implementation/README.md) |
| プロジェクト固有のハーネス | その製品をどう起動し、何を操作し、何をもって成功とするかを持つ仕組み。ライブラリ、ドライバー、テストデータ、再発防止チェックを含む。 | 対象リポジトリ側に置く。Garden には置かない。 | [docs/implementation/README.md](docs/implementation/README.md) |
| 育成 | トリガーを受けて AI が、人に任された範囲で実装と検証を繰り返すこと。 | 人の判断（完成・出荷）は含まない。 | [docs/philosophy.md](docs/philosophy.md) |
| 収穫 | ガーデンが仕様を満たしたと判断し、成果物と検証結果を添えて人に知らせること。 | 「収穫を知らせる」のはガーデン、「完成を決める」のは人。収穫＝出荷ではない。 | [docs/philosophy.md](docs/philosophy.md) |
| 完成・出荷の判断 | 「欲しかったものができた」と人が認め、出荷するか、さらに手を入れるかを決めること。 | AI には委ねない。 | [README.md](README.md), [docs/philosophy.md](docs/philosophy.md) |
| 還元 | 一つの庭で得た知見のうち、他の庭でも役立つと確かめられたものを、有効な条件とともに Garden のテンプレートや手順に取り込むこと。 | 確かめる前の知見は還元しない。 | [README.md](README.md), [docs/philosophy.md](docs/philosophy.md) |
| トリガー | 作業の依頼や変更の到着など、育成を始めるきっかけとなる出来事。 | 現時点の Garden には実装されていない（Phase 3 で扱う）。 | [docs/philosophy.md](docs/philosophy.md) |

## CLI とカタログ

`garden.py` が提供するのは `list` と `check` の二つだけです。

| 用語 | 定義 | 使い分け・注意 | 出典 |
| --- | --- | --- | --- |
| カタログ（`garden.toml`） | 対象リポジトリの登録一覧。`[repositories.<id>]` ごとに `path`、`remote`、`description`、`check` を持つ。 | 「登録一覧」と同義。現在は空。`--catalog` で別ファイルを指定できる。 | [docs/implementation/README.md](docs/implementation/README.md) |
| リポジトリ ID | カタログ上の安定した識別子（`<repo-id>`）。`check` の引数に渡す。 | リポジトリ名と一致していなくてもよい。 | [AGENTS.md](AGENTS.md) |
| ワークスペース | カタログの `path` を解決する基準ディレクトリ。既定は Garden の親ディレクトリ（`--workspace`）。 | Garden と対象リポジトリを同じ親に並べる前提。 | [garden.py](garden.py) |
| チェック（登録済みチェック） | 対象リポジトリで既に使っている自動チェックのコマンド。カタログの `check` に引数の配列で登録し、作業ツリーのルートで実行する。 | 「検証」の一部だが同義ではない。チェックは終了コードで合否が決まるコマンド。 | [docs/implementation/README.md](docs/implementation/README.md) |
| `passed` | チェックコマンドが終了コード 0 で終わった。 | 仕様を満たしたことは意味しない。 | [docs/implementation/README.md](docs/implementation/README.md) |
| `failed` | チェックコマンドが非ゼロで終了した。 | 原因は標準エラーのログで確認。 | [docs/implementation/README.md](docs/implementation/README.md) |
| `unavailable` | チェックを実行できなかった。チェックアウトがない、取得 URL が不一致、Git 情報を読めない、コマンドを起動できない、など。 | `failed` と区別する。「落ちた」と一括りにしない。 | [docs/implementation/README.md](docs/implementation/README.md) |
| 取得 URL（`remote`） | `git remote get-url <remote-name>` が返す fetch 用 URL。いずれかのリモートと一致したときだけチェックを実行する。 | push 用 URL ではない。 | [docs/implementation/README.md](docs/implementation/README.md) |
| `dirty` | チェック開始前に未コミットの変更や未追跡ファイルがあったか。 | `status.showUntrackedFiles=no` の設定でも未追跡ファイルを数える。 | [garden.py](garden.py), [test_garden.py](test_garden.py) |

## 仕様と検証

「検証」は幅の広い言葉なので、何をどの前提で確かめたかを添えて使います。

| 用語 | 定義 | 使い分け・注意 | 出典 |
| --- | --- | --- | --- |
| 仕様 | 誰のどんな願いをかなえるか、何ができ、何が起きてはいけないかを人が定めたもの。受け入れ条件と不変条件を含む。 | 人が最も力を注ぐ部分。AI が仕様を決められないときは人に問いを返す。 | [docs/philosophy.md](docs/philosophy.md) |
| 受け入れ条件 | 実際の振る舞いで完成を判断できる条件。検証はここから組み立てる。 | 「完成の条件」「完了条件」とほぼ同義。AI が実装に合わせて作ったテストはこれに含めない。 | [docs/philosophy.md](docs/philosophy.md) |
| 不変条件 | 常に守るべき性質。仕様に明示し、必要に応じて形式検証で確かめる。 | 「守るべき性質」と同義。人が定める。 | [docs/philosophy.md](docs/philosophy.md) |
| 検証 | 仕様（受け入れ条件・不変条件）と実際の振る舞いを照らし合わせること。チェック、実行時の確認、形式検証を含む。 | 結果には、どの性質をどんな前提で確かめたかを残す。「テストが通った」だけで検証済みと言わない。 | [docs/philosophy.md](docs/philosophy.md) |
| 再発防止チェック | 実際の失敗やレビュー指摘を、型、lint、テスト、API 設計に変えて、次は自動で防ぐか検出できるようにしたもの。 | 対象リポジトリに置く。過去の失敗を再現して検出できることを確かめる。 | [docs/implementation/README.md](docs/implementation/README.md) |
| 証拠 | 確認した条件、手順、結果、未確認の点。レビューや収穫の報告に添える。 | 「動作の証拠」「検証の根拠」も同じ意味。 | [docs/implementation/README.md](docs/implementation/README.md) |
