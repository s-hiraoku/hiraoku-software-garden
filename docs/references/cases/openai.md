# OpenAI — Harness Engineering と Symphony

## エージェントが動作を確かめる環境

OpenAIのHarness Engineeringの記事について、共有年表では、エージェントがバグを再現し、修正前後の動作を記録し、PRとCIの結果を追えるようにした事例が紹介されています。エージェントが実装から検証へ進むには、プロダクトを起動・操作でき、結果を観測できる環境がどこまで必要かを考える材料になります。[共有年表](../software-factory-timeline.md) · [Harness Engineering](https://openai.com/index/harness-engineering/)

## チケットに応じて仕事を割り当てるSymphony

Symphonyの公開仕様は、イシュートラッカーを定期的に読み取り、着手できるチケットごとに作業環境を用意してエージェントに割り当てるサービスを定義します。リポジトリの `WORKFLOW.md` にワークフローのプロンプトと実行設定を記録します。そのリポジトリで何をもって引き渡しとするかも定義でき、人のレビュー待ちが処理の到達点になる場合があります。[Symphony仕様の概要](https://github.com/openai/symphony/blob/be10a1b79df723d6d7612b5651c8522704dafb2e/SPEC.md#1-problem-statement)

オーケストレーターは、割り当て済みか、再試行待ちか、実行中かを管理し、二重起動を防ぎます。定期確認ではチケットの現在の状態を照合し、対象外になった仕事を止めます。この仕様例では、再起動時はトラッカーとディスク上の作業環境から回復します。[Symphony仕様の状態遷移と再起動](https://github.com/openai/symphony/blob/be10a1b79df723d6d7612b5651c8522704dafb2e/SPEC.md#7-orchestration-state-machine)
