# 仕様を計画・実装・検証につなげる

## GitHub Spec Kitの仕様駆動開発

GitHub Spec Kitの仕様駆動開発では、まず何を作るかとその理由を定義し、仕様を計画、タスク、動くコードにつなげます。プロジェクト全体の原則を一度定め、機能ごとに仕様化と実装の手順を繰り返します。公開例では、各段階の結果を人が確認して進み、実装後に仕様との整合を確認する `converge` の工程を置いています。[Spec KitのSpec-Driven Development](https://github.com/github/spec-kit/blob/a4d571229e164e440f8950cac034607bae7b7518/README.md#spec-driven-development)

同じ資料には、既存の不具合を調査・修正・再検証する手順と、アイデアを実装に進めるか判断する手順もあります。作りたいものを具体化する仕事と、既知の期待に戻す仕事とで、仕様化の深さを変える例として読めます。[Spec Kitの工程一覧](https://github.com/github/spec-kit/blob/a4d571229e164e440f8950cac034607bae7b7518/README.md#choose-your-process)

## ガーデンの仕様を考えるときの問い

- 誰のどんな目的を実現するのか。実際の挙動で受け入れを判断できる条件は何か。
- 必ず守るべき性質は何か。想定する環境や使い方の前提は何か。
- 整合性の確認で仕様の曖昧さが見つかった場合、どの決定を人に返すか。

仕様と実装の両方に同じ思い違いが入るリスクや、不変条件を人が定める理由は、共有資料で詳しく述べられています。[仕様をつくる人の役割と形式検証](../specification-and-formal-verification.md)
