<!-- garden:v1 kind=escalation run_id=20261123T114214Z-beaf6ea5 idempotency_key=beaf6ea521810a24 verdict=INCONCLUSIVE commit=0a8f0d5 -->
### [Garden] 人の確認が必要です（`verify_relay` / `INCONCLUSIVE`）

- **対象版**: `0a8f0d5` / **承認済み仕様**: `message-relay` v1 (`15c5538`)
- **判定**: `INCONCLUSIVE`（所要 0.5s / 再試行 0回 / 費用 \$0.00）

| 条件 ID | 判定 | 根拠チェック | 備考 / 未確認の点 |
| --- | --- | --- | --- |
| `AC-001` | `inconclusive` | `toolchain` | exited with inconclusive code 75 |
| `AC-002` | `not_evaluated` | なし | not executed because an earlier step halted the flow |
| `INV-001` | `not_evaluated` | なし | not executed because an earlier step halted the flow |
| `INV-002` | `not_evaluated` | なし | not executed because an earlier step halted the flow |

#### 人に求める判断 / 詳細
- **理由**: `exit_code_inconclusive`
- **詳細**: exited with inconclusive code 75
