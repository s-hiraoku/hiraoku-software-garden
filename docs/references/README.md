# ソフトウェアファクトリーの参考資料

人が仕様を定めるところから、エージェントによる実装・検証、出荷とその後の改善までを考えるための資料集です。ソフトウェアガーデンとして定めた方針は [ソフトウェアガーデンの思想](../philosophy.md) にあります。

## 実践事例と公開仕様

| 資料 | 読める観点 |
| --- | --- |
| [StrongDM — Software Factory と Attractor](cases/strongdm.md) | 実装とシナリオによる検証、工程ごとの失敗・再試行・承認 |
| [OpenAI — Harness Engineering と Symphony](cases/openai.md) | エージェントが動作を確かめられる環境、チケットの割り当て、実行の回復 |
| [Matt Pocock — Software Factory](cases/matt-pocock-software-factory.md) | 仕事とトリガーの定義、ランナー、人が判断する位置 |
| [Anthropic — Claude Code Action](cases/anthropic.md) | GitHub のイベントから始めるレビュー、定期保守、仕事に応じた権限 |
| [Warp — Warp Factories](cases/warp.md) | 対象リポジトリやエージェントの設定をコードで持つ考え方、実行結果からの改善 |
| [Lauren Tan — All Code Is Slop (Except Mine)](cases/lauren-tan.md) | コードと開発環境への投資、決定的なチェック、実際の挙動から得る証拠 |

## 仕様・検証・出荷の手法

| 資料 | 読める観点 |
| --- | --- |
| [仕様を計画・実装・検証につなげる](methods/specification.md) | GitHub Spec Kit の仕様駆動開発と、実装に渡す前に人が決めること |
| [形式検証の入口](methods/formal-verification.md) | TLA⁺・TLC、Quint、Lean 4 が扱う問いと、それぞれで得られる証拠 |
| [出荷後の確認と改善](methods/release-and-improvement.md) | Argo Rollouts の段階的な公開、DORA のデリバリー指標 |
| [開発から本番運用までの検証](sources/all-code-is-slop/verification-lifecycle.md) | 本番前、デプロイ、リリース、運用中に確かめること |

## 保存資料

英語の文字起こし、日本語訳と要約、照合メモを収録しています。[All Code Is Slop (Except Mine) の講演資料](sources/all-code-is-slop/README.md)

## 横断メモと共有資料

- 複数の事例で重視される品質ゲート、トリガー、測定を比較したメモです。[ソフトウェアファクトリーの基礎とソフトウェアガーデンの優先順位](software-factory-foundations.md)
- StrongDM、Ralph Wiggum、Dark Factory、Harness Engineering、Symphonyなどの動きを時系列で追った共有資料です。[Software Factoryの変遷](software-factory-timeline.md)
- 仕様に人が労力をかける理由と、形式検証を取り入れたい背景をまとめた共有資料です。[仕様をつくる人の役割と形式検証](specification-and-formal-verification.md)
