# Warp — Warp Factories

Warpは公開リポジトリのREADMEで、自分たちのリポジトリの開発にWarp Factoriesを使い、エージェントがイシューの整理、仕様の作成、実装、PRレビューを担うと説明しています。ワークフローをコードとして定義し、評価やベンチマークを使って改善するという考え方です。[Warpの公開リポジトリ](https://github.com/warpdotdev/warp/blob/f8223cef2d167a5858bf233df8a1fb6f74537fa5/README.md#automate-development-with-warp-factories)

同リポジトリのテスト用 `factory.yaml` には、対象リポジトリの一覧、エージェントの既定値、実行時のフックの定義例があります。ファクトリー全体と個々のエージェントに対し、どの設定を共有するかを考える参考になります。フックのテストでは、この設定例に対応する一覧をテストコードで組み立てています。[Warpのテスト用定義](https://github.com/warpdotdev/warp/blob/f8223cef2d167a5858bf233df8a1fb6f74537fa5/crates/ai/src/hooks/fixtures/factory_layout/acme-factory/factory.yaml)

導入を一つの仕事から広げる順序や、実行結果を設定やスキルの改善に還元する考えは、リポジトリに保存されている比較メモにもまとまっています。[ソフトウェアファクトリーの基礎とソフトウェアガーデンの優先順位](../software-factory-foundations.md)
