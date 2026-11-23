# StrongDM — Software Factory と Attractor

## 仕様から検証までの実践

StrongDMのSoftware Factoryについて、共有年表では、エージェントが仕様に沿って実装を繰り返し、実装リポジトリの外に置いたシナリオと、SlackやOktaなどの挙動を再現した環境で結果を確かめる事例が紹介されています。実装を作る工程と、利用者のシナリオで実装を評価する工程の設計を考える際に参照できます。[共有年表](../software-factory-timeline.md) · [StrongDM Software Factory](https://factory.strongdm.ai/)

## Attractorの公開仕様

StrongDMは、AIを使う複数の工程をどうつなぐかについて、Attractorの仕様を公開しています。Graphviz DOTで実装、検証、承認などの工程と、実行結果に応じた遷移を定義します。工程の完了ごとにチェックポイントを保存し、中断後はそこから再開します。人の選択を待ってから遷移を決める工程も定義できます。[Attractor仕様の設計原則](https://github.com/strongdm/attractor/blob/fb57a55ed97372a27ac90102f436947e29f48426/attractor-spec.md#13-design-principles)

`goal_gate` は、実行済みの重要な工程の結果を終了時に確認する仕組みです。条件を満たさない場合には、ワークフロー定義に沿って再試行先へ戻ります。同仕様は部分成功も扱うので、何を成功に含めるかと、必須の検証を必ず通る経路をどう定義するかが読みどころです。[Attractor仕様のGoal Gate Enforcement](https://github.com/strongdm/attractor/blob/fb57a55ed97372a27ac90102f436947e29f48426/attractor-spec.md#34-goal-gate-enforcement)
