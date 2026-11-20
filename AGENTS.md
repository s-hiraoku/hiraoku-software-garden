# Hiraoku Software Garden

Garden は複数リポジトリで作業するための Home Repo です。[設計と使い方](docs/implementation/README.md)を参照してください。

- `python3 garden.py list` で対象リポジトリを探します。登録先とチェックの呼び出しは [garden.toml](garden.toml) にあります。
- 対象リポジトリで作業する前に、そのリポジトリの指示と作業手順を確認します。
- `python3 garden.py check <repo-id> [<repo-id> ...]` で登録済みのチェックを実行します。出力を確認し、対象のリポジトリとコミットを結果に添えます。
- 複数リポジトリで使える手順は Garden に置きます。対象固有の制約と検証ロジックは対象リポジトリに置きます。
