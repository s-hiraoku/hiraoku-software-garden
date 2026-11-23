# All Code Is Slop (Except Mine) — 講演資料

- 講演者: Lauren Tan（ハンドル: poteto）
- 動画: https://www.youtube.com/watch?v=O_zcc6Xl12g
- 既存資料に記載された出典: https://ai.engineer/code/2026/talks/lauren-tan
- 関連プロジェクト: https://github.com/s-hiraoku/hiraoku-software-garden

## ファイル一覧

| ファイル | 用途・由来 |
| --- | --- |
| [summary-ja.md](summary-ja.md) | 既存の日本語要約。要点を短く読む |
| [transcript-ja.md](transcript-ja.md) | 2026-11-21 に提供された講演の日本語訳全文 |
| [verification-lifecycle.md](verification-lifecycle.md) | 講演で示された本番前から本番運用までの検証手法と、Garden での使い方 |
| [software-factory-notes.md](software-factory-notes.md) | 「ミシュランの厨房」の比喩と、エージェントが作業しやすい開発環境を整えるための参考メモ |
| [provided-ja.md](provided-ja.md) | 2026-11-20 にユーザーから受領した日本語講演テキスト。本文は原状保存 |
| [O_zcc6Xl12g_transcript.txt](O_zcc6Xl12g_transcript.txt) | 今回添付された英語の自動生成字幕。動画内時刻付き |
| [transcript-full.md](transcript-full.md) | 既存の英語全文。段落化され、配信時刻とクリップ相対時刻付き |
| [transcript-segments.txt](transcript-segments.txt) | 既存の英語字幕セグメント。細かい時刻を照合するための資料 |

英語資料は異なる字幕版です。時刻の基準や認識結果に差があるため、時刻だけで同一箇所と判断せず、発言内容で照合します。自動生成字幕の誤認識は原文の一部として保存しています。

## 日本語テキストを読む際の注意

2026-11-20 に受領した [provided-ja.md](provided-ja.md) について、添付の英語字幕と既存の英語全文に照らした注意点をまとめます。

- 「70%のトークンを検証に使う」は、講演で紹介された Cindy の数値です。Lauren 自身は、自分もトークン使用の大半を検証に使うと述べています。添付字幕の 12:05–12:20、既存全文の 12:13 付近を参照。
- 個人の 3,000 PR と、チームの 11,000 PR は対象が異なります。両方を同じ指標として扱わないようにします。
- 日本語本文の「私もの」、Potato、所属表記などは受領時のままです。ハンドルの表記は既存資料では poteto です。自動字幕の SpaceXSI 等の表記だけから所属の正式名称を確定しません。
- `useEffect` やコメントの禁止は、講演で述べられた特定の開発環境の実例です。このリポジトリへの一律適用は決定していません。
- PR 数、コントリビューター数、リバート率、性能改善は講演中の報告として読みます。この整理では独立した測定や外部検証は行っていません。

## このプロジェクトへの接続

[Home Repo の設計と使い方](../implementation/README.md)で、講演の知見を複数リポジトリの開発に適用する方法を説明しています。

[ドキュメント一覧に戻る](../README.md)
