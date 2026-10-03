# PHASE 5A.5 PRE-OPTIMIZATION AUDIT

Baseline task: `19293f3c-2d73-496a-a7d4-47421a178682`; 19 observed model starts, matched 19 ends.

| Call | Agent | Observed node namespace |
|---:|---|---|
| 1 | supervisor | query_analyzer:1ac59f1d-3209-e3b7-2069-46381d820a5f |
| 2 | supervisor | orchestration_router:8cbe436a-cd7a-068b-7332-e5b87f38e15d |
| 3 | supervisor | supervisor_plan:b266488a-8df2-661e-31bf-1b2c9ae05bf8 |
| 4 | data | agent_worker:0f9b459d-d2be-b0a7-bfe6-ab26bc56c793|plan:f0428b71-e125-23be-2155-d5cb96956da4 |
| 5 | research | agent_worker:85b534cd-aa4f-4c0c-67b1-3f14a3b44aa1|plan:40bbc30f-95d9-2f6b-5861-29dd5d39ab48 |
| 6 | data | agent_worker:0f9b459d-d2be-b0a7-bfe6-ab26bc56c793|work:6dba8538-4c9b-7a23-b298-7194d57764ca |
| 7 | research | agent_worker:85b534cd-aa4f-4c0c-67b1-3f14a3b44aa1|contract:f5c4e63f-4342-b40c-cce4-bc7e341ad306 |
| 8 | data | agent_worker:0f9b459d-d2be-b0a7-bfe6-ab26bc56c793|contract:a4e5f439-8812-0f7d-382e-c42d274925c9 |
| 9 | supervisor | draft_synthesis:584cebfa-4107-4494-591c-c54f4c1f5cb5 |
| 10 | reviewer | review_agents:a45e5a21-f460-a516-75c6-57a2d386b657|review:408365ff-7ea7-fab4-9897-f32bb46b9d89 |
| 11 | data | agent_worker:e8f67952-6b1b-c133-45c0-a2cb171ed213|plan:b6fc0553-6702-7216-ea6f-781eccfba75d |
| 12 | data | agent_worker:e8f67952-6b1b-c133-45c0-a2cb171ed213|work:a73765d2-5cce-32af-afec-45bd5a4edce1 |
| 13 | data | agent_worker:e8f67952-6b1b-c133-45c0-a2cb171ed213|contract:77354f76-06e1-b65e-b697-d56e34469016 |
| 14 | supervisor | draft_synthesis:f4235868-e6fe-9f0a-8f15-1dbd4da92d9a |
| 15 | supervisor | generator:d2bbad70-5d96-6630-6ad0-b647162d1c8f |
| 16 | supervisor | verifier:071cec35-ccb3-95b4-fa7a-7f21c8bedc5b |
| 17 | supervisor | answer_revision:e693aa59-f9bd-8a29-c7e8-a31a6ec46f5b |
| 18 | supervisor | verifier:7486282f-f3f7-3983-6255-68d8322172ff |
| 19 | supervisor | extract_memory:de3ed387-1e84-a17b-37f4-bf9e2a936f7f |

Root/Supervisor=10, Research=2, Data=6, Coding=0, Reviewer=1.
Final synthesis/grounding/revision = generator1 + verifier2 + answer_revision1 =4.
Root also query_analyzer1, router1, plan1, draft_synthesis2, memory1.
Data initial plan/code-generation/contract=3; supplemental plan/code-generation/contract=3.
Five tools: Research workspace.search1; Data initial inspect+analyze2; Data supplemental inspect+analyze2.

Result merge, registry selection, evidence-ID and citation validation already deterministic.
The second Data inspection reread the same dataset; second Data analysis repeated the same
core table/selection/chart work. No second workspace retrieval in this baseline.
Both drafts and final synthesis consumed the same result/evidence references.
Research objective added architecture/training details absent from fixture sources; Reviewer
asked for these as gaps and a validated chart description. Scientific visual validation was
never part of the artifact execution contract. Data initial valid computed output became
PARTIAL after ValueError in summary validation. Raw invalid model reply was intentionally
not persisted, so the exact invalid field/reference cannot be recovered honestly.

## Actual F supplementary 0/2 causes

### research — PARTIAL

Objective: Fill only this review gap: research: missing evidence or incomplete analysis. Address only: research: missing evidence or incomplete analysis. Which uploaded documents (three papers) were in scope, and what are their document/artifact IDs?; For each paper: proposed method name, architectural/config design elements, claimed design advantages, total parameter budget, and reported performance metric values (e.g., mIoU) — none retrievable from evidence in this delegation.; Whether the three paper fixtures were actually attached to the workspace or only referenced by memory.; Whether the 'Explicit smoke evidence-gap fault' is a transient retrieval fault or a permanent absence of the source documents (needs Supervisor re-dispatch or a vali

Unresolved: ["Which artifact ID (4d60c4ea-a4c6-417b-8f0f-a9c505d73eb8 or 89024b8d-0080-4af1-a81b-7ccfef453c9a) corresponds to which of the three papers, and what is the third paper's ID (only two artifact IDs are present for three papers)?"]

### data — PARTIAL

Objective: Fill only this review gap: data: missing evidence or incomplete analysis. Address only: data: missing evidence or incomplete analysis. Summary unavailable: ValueError

Unresolved: ["Which three uploaded paper documents exist in the workspace, and what methods/design advantages do they report? No paper-side evidence was collected in this delegation.", "Was the stated chart artifact (comparison.png) and summary.json actually persisted? No artifact IDs were returned; artifact_ids is empty.", "Does the 'under 3M parameters' requirement mean strict < 3.0M or ≤ 3.0M? The computed subset used strict < 3.0; either interpretation excludes Model B (3.2M), but the criterion wording is not evidenced.", "What constitutes a fair comparison between paper-reported numbers and this fixture (identical training protocol, splits, metric definitions)? No evidence of protocol or metric alignment was collected.", "NameError"]

Research: whole original delegation failed under explicit evidence-gap injection;
supplement retrieved actual source evidence but could not satisfy expanded architecture/training
requirements absent from synthetic papers. Classification: bad/overbroad objective and insufficient
source facts, not wrong agent/capability/tool or timeout.
Data: initial structured contract ValueError despite completed execution; supplemental generated
code failed then repaired code failed with `NameError: isinstance is not defined`.
Classification: bad result contract + restricted-code failure; vague gap also expanded scope.
Neither supplementary completion was caused by an unavailable agent, missing tool or timeout.

## Decisions before implementation

Remove clear-case analyzer/router/worker-planner calls; use schema-gated deterministic pandas
operations and authoritative Data result contracts. Replace provisional model draft with typed
summary projection. Retain final synthesis, semantic grounding, optional revision, root memory
and risk-triggered Reviewer. Scope missing work precisely, consult existing evidence first and
cache only completed task-local results with source/context/contract versions.
Budgets reserve finalization calls and persist atomic reservations before concurrent attempts.
Existing checkpoints/policy/HITL remain authoritative. No new agents or integrations.
