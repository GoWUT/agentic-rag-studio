# PostgreSQL Migration Report

Mode: dry-run
Outcome: DRY RUN VALIDATED
Source schema versions: {'existing-sessions.sqlite3': 0, 'existing-indexes.sqlite3': 0}
Destination revision: 5b1_0001
Foreign key validation: PASS

| Table | Source rows | Destination rows | Verified | Source checksum |
| --- | ---: | ---: | --- | --- |
| sessions | 23 | 0 | NOT RUN | 58352e2cfcd8c818e83794b723fa13d14848f8f7320f312edcf228c7f3f6bd14 |
| workspaces | 3 | 0 | NOT RUN | d1f57f7e1b0d5961383e765cfb948e53c73041af7f0d6d3bced2da5a21eac7f9 |
| workspace_documents | 5 | 0 | NOT RUN | 99ef36828d3600fe7b2e629cb806a5022c0f68089a73f91e1b3076cf10984268 |
| long_term_memories | 8 | 0 | NOT RUN | ef6452bc79383a2edb390c2874688927cbedd397a7699be12daa0f5f7404e47f |
| data_assets | 2 | 0 | NOT RUN | b275a66d1713dd06bfae926da18d81b33c856ab54692e942732d23d488e9df9c |
| analysis_executions | 18 | 0 | NOT RUN | eb04ec2eca0fcb436cdeb256fa0c6f8cc05b3497c9ab1c0e1025f108649fc148 |
| artifacts | 12 | 0 | NOT RUN | 87a3725363be3d0ee041c6b9867c50b0bd5c82f27c63c533b6edd074f361dc21 |
| tasks | 6 | 0 | NOT RUN | 316fed56f9c2e138af8eafe7c8d6533adac2c90e71ddad5ba65c26b6e8c90890 |
| task_steps | 26 | 0 | NOT RUN | 727a2a627bca9b019ea72d13836030d7bf0fae92baa72da17cca5c66c9ff7867 |
| task_events | 115 | 0 | NOT RUN | f91885f7c5e187509cb1a5d640df8c2be437a2179b303c60df9d44d60e4bd6d3 |
| tool_executions | 0 | 0 | NOT RUN | 4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 |
| approval_requests | 0 | 0 | NOT RUN | 4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 |
| mcp_servers | 0 | 0 | NOT RUN | 4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 |
| agent_runs | 0 | 0 | NOT RUN | 4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 |
| agent_delegations | 0 | 0 | NOT RUN | 4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 |
| indexes | 4 | 0 | NOT RUN | 73e00d39cd97d6fa10c8d3c485f03adfeffd46f4cc5bcbb219c9f628065b61b0 |

Warnings:
- existing-sessions.sqlite3: ignored non-application table checkpoints
- existing-sessions.sqlite3: ignored non-application table writes
