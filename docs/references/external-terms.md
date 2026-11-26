# 外部参考用語集（参考事例・外部ツール）

ここには、外部の資料・講演・ツールで定義された用語をまとめています。hiraoku-software-garden 自身のユビキタス言語は [用語集（GLOSSARY.md）](../../GLOSSARY.md) を参照してください。

意味は出典の定義に従い、見分けやすいように要点とソフトウェアガーデンとの関係だけを書いています。ソフトウェアガーデンで採用しているもの（現状は `pstack` のみ）と、参考にとどめているものを区別して使います。

| 用語 | 定義 | ソフトウェアガーデンとの関係 | 出典 |
| --- | --- | --- | --- |
| pstack | Lauren Tan が公開した開発スキル集。`/correct`、`/poteto-mode`、`create-verification-skill`、`maintain-verification-skill` などを含む。 | **採用済み**。共通の開発ハーネスとして [harnesses/pstack.toml](../../harnesses/pstack.toml) で `pinned_ref` を管理し、`garden doctor` で導入を確認する。 | [使い方ガイド](../guides/usage.md), [lauren-tan.md](cases/lauren-tan.md) |
| Home Repo Pattern | Matt Pocock が示した、複数リポジトリの作業を一つの拠点リポジトリから始めるやり方。 | ナーサリー構成の土台。ただし、このリポジトリは Home Repo とは呼ばず「ナーサリー」と呼ぶ。 | [使い方ガイド](../guides/usage.md) |
| ミシュランの厨房 | Lauren Tan の比喩。自動化より先に、コードベースの設計、型、lint、決定的なチェック、動作の検証を整える開発環境。 | 「品質ゲートを先に固める」というソフトウェアガーデンの優先順位の根拠。 | [lauren-tan.md](cases/lauren-tan.md) |
| 逆ピラミッド | Tan の優先順位。①コードベースと API の改善、②lint や型などの静的チェック、③文章のルールやスキル、④人間のレビュー。 | 参考（設計書 §5「信頼と改善」の指針）。 | [software-factory-timeline.md](software-factory-timeline.md), [lauren-tan.md](cases/lauren-tan.md) |
| Dune | Grok Bot チームが Electron アプリ向けに作った、設計制約や lint を組み込んだフレームワーク（例: `useEffect` の禁止）。 | 参考。制約を API や lint に組み込む実例。 | [lauren-tan.md](cases/lauren-tan.md) |
| タスク / run / ランナー / オーケストレーター | Pocock の `software-factory` スキルの語彙。繰り返す仕事、その一回分の実行、実行環境、トリガーを受けて仕事を割り振る役。 | 参考。ソフトウェアガーデンでは「フロー」「実行記録」「実行環境」「トリガー」として独自に定義している。 | [matt-pocock-software-factory.md](cases/matt-pocock-software-factory.md) |
| Sandcastle | `sandcastle.run()` で分離されたサンドボックス上のエージェントに作業を渡す TypeScript ライブラリ（Pocock）。 | 参考。 | [matt-pocock-software-factory.md](cases/matt-pocock-software-factory.md) |
| Ralph Wiggum | エージェントが小さなタスクを選び、実装し、型チェックやテストで確かめ、コミットすることをループで繰り返す使い方。 | 参考。 | [software-factory-timeline.md](software-factory-timeline.md) |
| Dark Factory | 仕様を入力するとソフトウェアができあがる最終段階を自動運転になぞらえた Dan Shapiro の言葉。 | 参考。ソフトウェアガーデンは最後の完成・出荷判断を人に残す点で異なる。 | [software-factory-timeline.md](software-factory-timeline.md) |
| Harness Engineering | OpenAI の記事。エージェントがバグの再現、修正前後の記録、PR と CI の追跡までできる環境を整える考え方。 | 参考。 | [openai.md](cases/openai.md) |
| Symphony | イシュートラッカーを読み、チケットごとに作業環境を用意してエージェントに割り当てる OpenAI の公開仕様。`WORKFLOW.md` で設定する。 | 参考。 | [openai.md](cases/openai.md) |
| Attractor / `goal_gate` | StrongDM の公開仕様。Graphviz DOT で工程と遷移を定義し、`goal_gate` で重要工程の結果を終了時に確認する。 | 参考。 | [strongdm.md](cases/strongdm.md) |
| Factories-as-code（`factory.yaml`） | Warp の考え方。ワークフロー、スキル、MCP、モデル選択などをコードとしてバージョン管理する。 | 参考。対象リポジトリの `.garden/garden.toml`（ガーデン定義）と発想が近い。 | [warp.md](cases/warp.md) |
| Spec Kit / `converge` | GitHub の仕様駆動開発ツール。仕様を計画、タスク、コードにつなげ、`converge` で実装と仕様の整合を確かめる。 | 参考。 | [specification.md](methods/specification.md) |
| DORA 指標 | 変更リードタイム、デプロイ頻度、復旧時間、変更失敗率、デプロイの手戻り率の五つ。 | 参考。将来の測定候補。 | [release-and-improvement.md](methods/release-and-improvement.md) |
| 自作自演のオールグリーン | AI が実装とテストの両方を作り、同じ思い違いが両方に入ったままテストが通ってしまう問題。 | 検証を人の承認済み仕様（Trust Anchor）から組み立てる理由。 | [specification-and-formal-verification.md](specification-and-formal-verification.md) |
| 決定的なチェック（品質ゲート） | 型、lint、静的解析、テストのように、制約に違反すると毎回同じように失敗するチェック。 | 文書のルールや人のレビューより先に整える。「決定論的」とも書かれる。 | [software-factory-foundations.md](software-factory-foundations.md) |
| TLA⁺ / TLC | 並行実行やイベントの順序が変わっても不変条件が守られるかを、有限モデルの状態探索で調べる仕様言語とモデル検査器。 | 形式検証ツールの候補（`formal_check` 対応）。対象はモデルであって実装ではない。 | [formal-verification.md](methods/formal-verification.md) |
| Quint | 状態遷移の仕様を書いてシミュレートし、不変条件が破れる実行を探す言語。Apalache で記号的モデル検査もできる。 | 形式検証ツールの候補（`formal_check` 対応）。反例の再現用シードが得られる。 | [formal-verification.md](methods/formal-verification.md) |
| Lean 4 | 関数やアルゴリズムが定義した前提のもとで性質を満たすことを証明する定理証明支援系。 | 形式検証ツールの候補（`formal_check` 対応）。証拠の範囲は命題の定義と仮定で決まる。 | [formal-verification.md](methods/formal-verification.md) |
| デプロイ / リリース / 運用中の検証 | PR や CI の後、配置・公開・運用の各段階で確かめること。 | 対象リポジトリの配布形態に合わせて設計する。 | [verification-lifecycle.md](sources/all-code-is-slop/verification-lifecycle.md) |
