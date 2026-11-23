# Anthropic — Claude Code Action

AnthropicのClaude Code Actionは、GitHubのイシューやPRで行う作業をGitHub Actionsとして定義できる実装です。コメントでのメンション、イシューの割り当て、明示的なプロンプトを渡す自動化などに対応します。エージェントの出力を構造化して、後続のステップに渡す例も公開されています。[Claude Code Actionの機能](https://github.com/anthropics/claude-code-action/blob/acfe7992852970efb4fee683d63d628e3e52df67/README.md)

公式の自動化例では、PRレビューはPRの作成・更新で起動し、リポジトリの内容を読み、PRに所見を書きます。定期保守の例はスケジュールや手動実行で起動し、リポジトリ・イシュー・PRに書き込める設定です。同じエージェントを呼ぶ場合でも、作業の種類からトリガーと書き込み先を決める実例として読めます。[Claude Code Actionの自動化事例](https://github.com/anthropics/claude-code-action/blob/acfe7992852970efb4fee683d63d628e3e52df67/docs/solutions.md)
