---
spec_id: nursery-cli
version: 1
status: approved
title: hiraoku-software-garden ナーサリー CLI の振る舞い仕様
owner: "@s-hiraoku"
---

## 目的
ナーサリー（hiraoku-software-garden）自身の CLI、決定論的ステートマシン、事後ガード、テンプレート導入・更新、および反映ゲートが、設計どおりの安全性と互換性を維持していることを自リポジトリのガーデンとして継続的に検証します。

## 受け入れ条件
- **AC-001**: 既存カタログ操作（`garden.py list` / `check`）が取得 URL 照合、`dirty` 検出、終了コード規約（`0` / `1` / `2`）の後方互換を維持する。
- **AC-002**: ガーデン定義の厳格検証、Trust Anchor 照合、`doctor`、`run`、`report`、`reflect`、`feedback`、実プロバイダーアダプター、全 4 テンプレート型の `init` / `upgrade` が単体・結合テストで期待どおりに動作する。

## 不変条件
- **INV-001**: ステートマシン、収穫ゲート、修正後キャッシュ無効化、再試行上限、`observe_only` 降格、委譲厳格化の安全不変条件が有界全状態探索で常に成立する。
- **INV-002**: 事後ガードの glob パス境界判定および工程⑥の反映ゲート判定が、禁止パスの見逃しや未承認条件での `auto_merge` 許可を決して起こさない。
