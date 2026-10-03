# PostgreSQL Migration Report

Mode: import
Outcome: VERIFIED
Source schema versions: {'migration-source.sqlite3': 0}
Destination revision: 5b1_0001
Foreign key validation: PASS

| Table | Source rows | Destination rows | Verified | Source checksum |
| --- | ---: | ---: | --- | --- |
| workspaces | 1 | 1 | PASS | 1948d392fcb61d9d5f37adbeea107b28f9a592bdda36f0016700f7a0a907befd |
| workspace_documents | 0 | 0 | PASS | 4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 |
| sessions | 1 | 1 | PASS | 1642025a1114af93b128bb3b2f888e6946d5264fb0863eb5cd704e4534b0fc0a |
| tasks | 1 | 1 | PASS | 32e4f356de170ea2709489669cbee23e6171ff79496efebfee62e41096d4c2e1 |
| task_steps | 1 | 1 | PASS | f4e1b4f3f94353594b630de0958b20cbd724dad83a236061ba26f9d65dc18a2a |
| task_events | 2 | 2 | PASS | 855a5392d2fa6d026473ac7e223b70f2ff1bd9c3426133653db9d42084e0c96a |
| long_term_memories | 1 | 1 | PASS | 540dc3fef1771d777f9fbddf3e2d3899d1c34e321a9aa178364d16fff485b408 |
| tool_executions | 1 | 1 | PASS | d26419f9a54269de3b146df04aed452de0d740d947de040c9bfef0fa6e102874 |
| approval_requests | 1 | 1 | PASS | 3a6226ff1d8f5c2e9142e5770f6cf7daa035ee6e6d7f09e0263c4b137b69252e |
| agent_runs | 2 | 2 | PASS | e3e2b3276f9c4ad3df3e28ead6bfbd723edd0bd3aec362caadfd3a5d0365656a |
| agent_delegations | 1 | 1 | PASS | 8e9c2a26b2f9a6cf47a765c5c18d6a9e427bb50fd2d1e7050d1986c8f57dde43 |
| data_assets | 0 | 0 | PASS | 4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 |
| analysis_executions | 0 | 0 | PASS | 4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 |
| artifacts | 0 | 0 | PASS | 4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 |
| mcp_servers | 0 | 0 | PASS | 4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 |
| indexes | 0 | 0 | PASS | 4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 |

Warnings:
- Source has no table data_assets; treated as empty
- Source has no table analysis_executions; treated as empty
- Source has no table artifacts; treated as empty
- Source has no table mcp_servers; treated as empty
- Source has no table indexes; treated as empty
