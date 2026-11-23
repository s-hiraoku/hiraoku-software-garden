# 出荷後の確認と改善

## Argo Rolloutsの段階的な公開

Argo Rolloutsでは、カナリア公開などで、どの指標をどの頻度で測り、何を成功・失敗とするかを `AnalysisTemplate` に定義します。個々の `AnalysisRun` は成功、失敗、判定不能として扱われ、その結果で公開を進めるか、中止するか、一時停止するかが決まります。例えば、新版に一部のトラフィックを流し、HTTPの成功率を観測して次の段階へ進むかを判断できます。[Argo RolloutsのAnalysis & Progressive Delivery](https://github.com/argoproj/argo-rollouts/blob/8c9c5ad2182ba7757bb6a35e79d2051afc31feee/docs/features/analysis.md)

ガーデンで人が完成と出荷を判断したあと、利用環境でどのように結果を観測するかを考える参考になります。デプロイ、リリース、運用中の検証で着目する点は、講演資料に付随するメモにもまとまっています。[開発から本番運用までの検証](../sources/all-code-is-slop/verification-lifecycle.md)

## DORAのデリバリー指標

DORAの公式ガイドは、変更リードタイム、デプロイ頻度、失敗したデプロイからの復旧時間、変更失敗率、デプロイの手戻り率の五つを扱います。手戻り率は、本番で起きた問題に対応するために計画外で実施したデプロイの割合です。ファクトリーの実行記録をデプロイ結果と結びつけると、どの仕事で時間がかかり、どの変更が再作業につながったかを追えます。[DORAのデリバリー指標ガイド](https://github.com/dora-team/dora.dev/blob/ce6b5112e41f9e4cc8a65b5e881a54ceee6e4e30/hugo/content/guides/dora-metrics/_index.md)
