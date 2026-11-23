# 用語集

人とエージェントがこのリポジトリについて話すときに、同じ言葉を同じ意味で使うための用語集です。定義はリポジトリ内の記述に沿っています。「使い分け・注意」列には、混同しやすい言葉と避けたい言い方を書いています。用語の意味を変えたり新しい用語を導入したりしたときは、この表も更新します。

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
| 自作自演のオールグリーン | AI が実装とテストの両方を作り、同じ思い違いが両方に入ったままテストが通ってしまう問題。 | 検証を人の仕様から組み立てる理由。 | [specification-and-formal-verification.md](docs/references/specification-and-formal-verification.md) |
| 決定的なチェック（品質ゲート） | 型、lint、静的解析、テストのように、制約に違反すると毎回同じように失敗するチェック。 | 文書のルールや人のレビューより先に整える。「決定論的」とも書かれる。 | [software-factory-foundations.md](docs/references/software-factory-foundations.md) |
| 再発防止チェック | 実際の失敗やレビュー指摘を、型、lint、テスト、API 設計に変えて、次は自動で防ぐか検出できるようにしたもの。 | 対象リポジトリに置く。過去の失敗を再現して検出できることを確かめる。 | [docs/implementation/README.md](docs/implementation/README.md) |
| 証拠 | 確認した条件、手順、結果、未確認の点。レビューや収穫の報告に添える。 | 「動作の証拠」「検証の根拠」も同じ意味。 | [docs/implementation/README.md](docs/implementation/README.md) |
| 形式検証 | モデル検査や定理証明で、定めた前提のもとで性質が常に成り立つことを確かめる手法。 | 実際のアプリがモデルどおり動くかは、別途テストや実行時の観測で確かめる。 | [formal-verification.md](docs/references/methods/formal-verification.md) |
| TLA⁺ / TLC | 並行実行やイベントの順序が変わっても不変条件が守られるかを、有限モデルの状態探索で調べる仕様言語とモデル検査器。 | 対象はモデルであって実装ではない。 | [formal-verification.md](docs/references/methods/formal-verification.md) |
| Quint | 状態遷移の仕様を書いてシミュレートし、不変条件が破れる実行を探す言語。Apalache で記号的モデル検査もできる。 | 反例の再現用シードが得られる。 | [formal-verification.md](docs/references/methods/formal-verification.md) |
| Lean 4 | 関数やアルゴリズムが定義した前提のもとで性質を満たすことを証明する定理証明支援系。 | 証拠の範囲は命題の定義と仮定で決まる。 | [formal-verification.md](docs/references/methods/formal-verification.md) |
| デプロイ / リリース / 運用中の検証 | PR や CI の後、配置・公開・運用の各段階で確かめること。 | プロジェクトの配布形態に合わせて設計する。 | [verification-lifecycle.md](docs/references/sources/all-code-is-slop/verification-lifecycle.md) |

## 参考事例・外部ツール

ここの用語は外部資料の言葉です。Garden で採用したもの（現状は pstack のみ）と、参考にとどめているものを区別して使います。

| 用語 | 定義 | Garden との関係 | 出典 |
| --- | --- | --- | --- |
| pstack | Lauren Tan が公開した開発スキル集。`/correct`、`/poteto-mode`、`create-verification-skill`、`maintain-verification-skill` などを含む。 | 採用済み。共通の開発ハーネス。配布やバージョン管理は未定。 | [docs/implementation/README.md](docs/implementation/README.md), [lauren-tan.md](docs/references/cases/lauren-tan.md) |
| Home Repo Pattern | Matt Pocock が示した、複数リポジトリの作業を一つの拠点リポジトリから始めるやり方。 | Garden の構成の土台。 | [docs/implementation/README.md](docs/implementation/README.md) |
| ミシュランの厨房 | Lauren Tan の比喩。自動化より先に、コードベースの設計、型、lint、決定的なチェック、動作の検証を整える開発環境。 | 「品質ゲートを先に固める」という Garden の優先順位の根拠。 | [lauren-tan.md](docs/references/cases/lauren-tan.md) |
| 逆ピラミッド | Tan の優先順位。①コードベースと API の改善、②lint や型などの静的チェック、③文章のルールやスキル、④人間のレビュー。 | 参考。 | [software-factory-timeline.md](docs/references/software-factory-timeline.md), [lauren-tan.md](docs/references/cases/lauren-tan.md) |
| Dune | Grok Bot チームが Electron アプリ向けに作った、設計制約や lint を組み込んだフレームワーク（例: `useEffect` の禁止）。 | 参考。制約を API や lint に組み込む実例。 | [lauren-tan.md](docs/references/cases/lauren-tan.md) |
| タスク / run / ランナー / オーケストレーター | Pocock の `software-factory` スキルの語彙。繰り返す仕事、その一回分の実行、実行環境、トリガーを受けて仕事を割り振る役。 | 参考。Garden にはまだ実装されていない。 | [matt-pocock-software-factory.md](docs/references/cases/matt-pocock-software-factory.md) |
| Sandcastle | `sandcastle.run()` で分離されたサンドボックス上のエージェントに作業を渡す TypeScript ライブラリ（Pocock）。 | 参考。 | [matt-pocock-software-factory.md](docs/references/cases/matt-pocock-software-factory.md) |
| Ralph Wiggum | エージェントが小さなタスクを選び、実装し、型チェックやテストで確かめ、コミットすることをループで繰り返す使い方。 | 参考。 | [software-factory-timeline.md](docs/references/software-factory-timeline.md) |
| Dark Factory | 仕様を入力するとソフトウェアができあがる最終段階を自動運転になぞらえた Dan Shapiro の言葉。 | 参考。Garden は最後の判断を人に残す点で異なる。 | [software-factory-timeline.md](docs/references/software-factory-timeline.md) |
| Harness Engineering | OpenAI の記事。エージェントがバグの再現、修正前後の記録、PR と CI の追跡までできる環境を整える考え方。 | 参考。 | [openai.md](docs/references/cases/openai.md) |
| Symphony | イシュートラッカーを読み、チケットごとに作業環境を用意してエージェントに割り当てる OpenAI の公開仕様。`WORKFLOW.md` で設定する。 | 参考。 | [openai.md](docs/references/cases/openai.md) |
| Attractor / `goal_gate` | StrongDM の公開仕様。Graphviz DOT で工程と遷移を定義し、`goal_gate` で重要工程の結果を終了時に確認する。 | 参考。 | [strongdm.md](docs/references/cases/strongdm.md) |
| Factories-as-code（`factory.yaml`） | Warp の考え方。ワークフロー、スキル、MCP、モデル選択などをコードとしてバージョン管理する。 | 参考。`garden.toml` はそのごく一部（登録一覧）に当たる。 | [warp.md](docs/references/cases/warp.md) |
| Spec Kit / `converge` | GitHub の仕様駆動開発ツール。仕様を計画、タスク、コードにつなげ、`converge` で実装と仕様の整合を確かめる。 | 参考。 | [specification.md](docs/references/methods/specification.md) |
| DORA 指標 | 変更リードタイム、デプロイ頻度、復旧時間、変更失敗率、デプロイの手戻り率の五つ。 | 参考。将来の測定候補。 | [release-and-improvement.md](docs/references/methods/release-and-improvement.md) |
