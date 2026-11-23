# ソフトウェアファクトリーの基礎と Garden の優先順位

## 発言者・組織ごとの着眼点

| 人・組織 | 重視する基礎 |
| --- | --- |
| Lauren Tan | 「ミシュランの厨房」として、コードベースのアーキテクチャ、型、lint、決定論的なチェック、動作の検証を自動化に先立って整えます。pstack はその手順を再利用するための一例です。 [講演と対談の参考メモ](lauren-tan-all-code-is-slop/software-factory-notes.md) |
| Matt Pocock | 仕事を起動するトリガーと実行の仕組みを「ソフトウェアファクトリー」と呼びます。共通の設計語彙を使い、一つのトリガーから始めます。Sandcastle v1 ではスキルを関数として呼び出す構想を示しています。 [Pocock の参考メモ](matt-pocock-software-factory.md) |
| Zach Lloyd（Warp） | ファクトリーをクラウド上のインフラとして扱います。定義をコードで持ち、実行結果を測定・評価し、設定やスキルの改善に使います。 [The Factory Stack](https://www.warp.dev/blog/the-factory-stack) · [Closing the loop](https://www.warp.dev/blog/agent-self-improving-software-factories) |
| SuperPlane | チケットの取り込みから実装、検証、レビュー可能な PR までをつなぐオープンソースの実装です。CI や承認を共通のワークフローに組み込みます。 [SuperPlane の概要](https://docs.superplane.com/get-started/overview/) |
| HumanLayer | Research / Plan / Implement の手順を再利用し、調査や計画などの成果物をチームで共有します。人間のレビューを、判断の効果が大きい段階に入れます。 [Advanced Context Engineering](https://www.humanlayer.dev/blog/advanced-context-engineering) · [HumanLayer](https://www.humanlayer.dev/) |
| Factory.ai | 企業で Droid を運用するための組織ポリシーや監査、利用状況とコストの測定を提供します。 [Factory for Enterprise](https://docs.factory.com/enterprise) |

Lloyd は、プロンプトをつないでクラウドエージェントを動かす方法から始めた組織が、規模を広げる段階で信頼性や測定に課題を抱えたと述べています。導入の順序としては、個別の小さな自動化、単一プロジェクトで一周するループ、蓄積したデータを使った改善を挙げています。 [本番運用向けの設計](https://www.warp.dev/blog/build-production-software-factory) · [段階的な導入](https://www.warp.dev/blog/adopting-the-software-factory-model-crawl-walk-run)

## 各資料に現れる設計項目

1. **決定論的な品質ゲート。** 型、lint、静的解析、テストで、守るべき制約や挙動に違反したときに失敗するようにします。
2. **スキルと手順の再利用。** 繰り返し使う指示や調査・計画・実装の進め方を、pstack や Research / Plan / Implement のように保守できる単位で残します。
3. **トリガーとループ。** イシュー作成、ラベル付け、スケジュール、CI の失敗を起点に作業を始めます。レビュー結果を修正に渡し、修正後に再検証します。Lloyd のいう closed-loop には、実行データから自動化そのものを改善する循環も含まれます。 [Closing the loop](https://www.warp.dev/blog/agent-self-improving-software-factories)
4. **Factories-as-code。** Warp の例では `factory.yaml` と関連ファイルで、ワークフロー、スキル、MCP、モデル選択などをバージョン管理します。エージェントが出した改善案も差分として評価・レビューできます。 [Warp の構成例](https://www.warp.dev/blog/software-factory-as-code)
5. **測定と評価。** 自分たちのリポジトリと作業データで、チェックの結果、手戻り、完了までの時間、費用を測ります。設定や手順を変えたときに、どの結果が変わったかを確かめられます。
6. **人が判断する箇所。** 作業を任せる判断、計画の承認、PR のレビューなど、どの段階で人の判断が必要かを決めます。

## Garden での適用案

**品質ゲートを先に固め、最初の検証から測定します。** 守るべき挙動や制約を一つ選び、違反時にチェックが失敗し、修正で通ることを確かめます。制約とチェックの実装は対象リポジトリに置き、Garden は共通の手順、チェックの呼び出し、結果の形式を持ちます。 [Home Repo の設計と使い方](implementation/README.md)

そのチェックを使って、トリガー一つと作業一種類のループを試します。例えば、定期実行で lint 違反を一件直し、チェック結果を添えた PR を人がレビューします。初回から成否、所要時間、人の手直しの有無を記録し、取得できる場合はトークンや実行費用も残します。この結果から、次に強くするチェックや広げる自動化を決めます。
