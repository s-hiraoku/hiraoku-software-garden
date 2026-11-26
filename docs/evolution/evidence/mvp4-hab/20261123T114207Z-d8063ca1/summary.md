<!-- garden:v1 kind=escalation run_id=20261123T114207Z-d8063ca1 idempotency_key=d8063ca12aba3413 verdict=UNSATISFIED commit=d980d5a -->
### [Garden] 人の確認が必要です（`verify_relay` / `UNSATISFIED`）

- **対象版**: `d980d5a` / **承認済み仕様**: `message-relay` v1 (`15c5538`)
- **判定**: `UNSATISFIED`（所要 4.0s / 再試行 0回 / 費用 \$0.00）

| 条件 ID | 判定 | 根拠チェック | 備考 / 未確認の点 |
| --- | --- | --- | --- |
| `AC-001` | `satisfied` | `toolchain` | なし |
| `AC-002` | `unsatisfied` | `relay_e2e` | command exited with code 1 |
| `INV-001` | `unsatisfied` | `relay_e2e` | command exited with code 1 |
| `INV-002` | `unsatisfied` | `relay_e2e` | command exited with code 1 |

#### 人に求める判断 / 詳細
- **理由**: `assertion_failed`
- **詳細**: command exited with code 1
