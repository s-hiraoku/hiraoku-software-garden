# Matt Pocock のソフトウェアファクトリー

## この言葉で指す変化

Matt Pocock（@mattpocockuk）は、名前が実態より大げさに聞こえ、流行や投資家向けの宣伝にも使われると認めています。そのうえで、開発の実際の変化を指す言葉として「ソフトウェアファクトリー」を支持しています。

> But it's also referring to a real change.

製品の機能実装やバグ修正にエージェントを使う仕事に、エージェントがそうした作業を進められるよう自動化を設計する仕事が加わった、という説明です。Pocock はこの役割を担う技術者を「factory engineers」と呼びます。 [2026年11月の投稿](https://x.com/mattpocockuk/status/2119748545691402733)

## トリガーで作業を始める

イシューの作成やラベル付け、定期実行、CI の失敗などをきっかけに、エージェントのセッションが起動します。人は、計画を承認するか、変更をマージできるかなど、判断が求められる場面を担います。 [AI Coding Dictionary](https://www.aihero.dev/ai-coding-dictionary/software-factory)

| きっかけ | 始まる作業 |
| --- | --- |
| イシューの作成やラベル付け | 調査や修正を行い、必要に応じて PR を出す |
| 定期実行（cron） | 定型の保守作業を行う |
| CI の失敗 | 原因を調べ、修正を提案する |

最初は一つの cron で一種類の作業を動かし、レビューできる PR を一件出す規模から始められます。例えば、毎晩 lint 違反を一件修正します。出てきた変更をレビューし、型、lint、テストなどの決定論的なチェックが機能することを確かめてから、自動化を広げます。 [AI Coding Dictionary](https://www.aihero.dev/ai-coding-dictionary/software-factory) · [設計スキルの Start slow](https://github.com/mattpocock/skills/blob/main/skills/in-progress/software-factory/SKILL.md#start-slow)

## 設計に使う共通語彙

`software-factory` スキルでは、繰り返す仕事をタスク、開始条件をトリガー、タスク一回分の実行を run と呼びます。チケットトラッカーは作業依頼を、Git ホストはブランチや PR を扱います。オーケストレーターはトリガーを受け取り、実行環境となるランナーに仕事を割り振ります。チームの課題に合わせて、こうした役割を担うサービスと実行するタスクを設計します。 [software-factory スキル](https://github.com/mattpocock/skills/blob/main/skills/in-progress/software-factory/SKILL.md) · [Software Factories for Real Engineers](https://www.aihero.dev/cohorts/software-factories-for-real-engineers-xdy1m)

## Sandcastle

Sandcastle は、`sandcastle.run()` で分離されたサンドボックス上のコーディングエージェントに作業を渡す TypeScript ライブラリです。2026年11月には v0.14.0 が公開されています。 [リポジトリ](https://github.com/mattpocock/sandcastle) · [v0.14.0 のリリース](https://github.com/mattpocock/sandcastle/releases/tag/v0.14.0)

Pocock が同月に示した v1 の構想では、コードレビューや仕様の実装といった各スキルを関数として呼び出せるようにし、既存のサンドボックスや Inngest、Trigger.dev などの実行基盤に組み込みます。 [Sandcastle v1 の構想](https://x.com/mattpocockuk/status/2116805878187794740)

## 一回の実行と人への引き渡し

Pocock の設計スキルでは、人の承認が必要になった時点でその実行を終えます。承認後にチケットや PR が更新されると、新しい実行が始まります。実行環境を使い捨てにできるよう、次の実行に渡す情報はチケット、Git ホスト、オーケストレーターに残します。仕事ごとに、何をきっかけに実行し、何に書き込み、次に何を始め、二重実行をどう防ぐかを決めます。[Pocock の設計スキル](https://github.com/mattpocock/skills/blob/abeeaf95660ba9e8b7c753eb38f441ca707a8823/skills/in-progress/software-factory/SKILL.md)

公開リポジトリの Sandcastle には、アプリケーションから `sandcastle.run()` を呼び、ローカル、コンテナ、クラウドの実行環境でエージェントを動かす実装があります。実行に使う環境とブランチ戦略をどう選ぶかを調べる入口になります。[Sandcastle の README](https://github.com/mattpocock/sandcastle/blob/1b380ca5e7a9fbece96c49d9edba264c1f512c4e/README.md)

## Lauren Tan との関係

Tan の「ミシュランの厨房」は、各コードベースのアーキテクチャ、決定論的なチェック、検証を整える視点です。Pocock の「ソフトウェアファクトリー」は、そうした環境で、仕事を起動してエージェントに任せる仕組みを設計する視点です。両者を合わせて読むと、検証できる環境を整え、結果を確認できる小さな自動化から広げる順序になります。 [Tan の参考メモ](lauren-tan.md)
