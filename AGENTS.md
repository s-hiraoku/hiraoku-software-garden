# Hiraoku Software Garden

Garden は複数リポジトリで作業するための Home Repo です。[設計と使い方](docs/implementation/README.md)を参照してください。用語の意味は[用語集](GLOSSARY.md)に合わせます。

- `python3 garden.py list` で対象リポジトリを探します。登録先とチェックの呼び出しは [garden.toml](garden.toml) にあります。
- 対象リポジトリで作業する前に、そのリポジトリの指示と作業手順を確認します。
- pstack を全プロジェクト共通の開発ハーネスとして使います。タスクの目的と完了条件を明確にし、小さく実装して、期待する挙動が確認できる証拠を残します。
- `python3 garden.py check <repo-id> [<repo-id> ...]` で登録済みのチェックを実行します。出力を確認し、対象のリポジトリとコミットを結果に添えます。
- 複数リポジトリで使える手順は Garden に置きます。対象固有の制約、ライブラリの選定と設定、検証ロジックは対象リポジトリに置きます。
