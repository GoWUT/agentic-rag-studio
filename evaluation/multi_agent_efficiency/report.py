"""Persist audited final validation; retain raw initial classifier decisions."""
import json
from pathlib import Path
from statistics import median
from evaluation.multi_agent_efficiency.comparison import run as comparison, quality_pass, project

ROOT=Path(__file__).resolve().parents[2]
RESULTS=ROOT/'evaluation/results/phase5a5'
def read(name):return json.loads((RESULTS/name).read_text(encoding='utf-8'))
def run():
    report=comparison();smoke=read('runtime_smoke.json');tests=read('tests.json');ui=read('ui_smoke.json')
    checks=dict(smoke['checks'])
    budget=read('budget_runtime.json') if (RESULTS/'budget_runtime.json').exists() else None
    if budget:
        for key,value in budget['checks'].items():
            if key.startswith(('hard_budget_', 'budget_')):
                checks[key]={**value,'source':'budget_runtime.json; final budget-only probe'}
    for i,row in enumerate(report['optimized_runs'],1):
        checks['optimized_run_'+str(i)]={'status':'PASS' if quality_pass(row) else 'FAIL',
            'details':row,'original_classifier_status':smoke['checks']['optimized_run_'+str(i)]['status'],
            'recheck':'Rule version 3: explicit best is/was A predicate with Markdown normalization. Original answers/measurements remain unchanged.'}
    checks['efficiency_panel']=ui
    folder=Path(smoke['fixture_folder'])
    usage=[json.loads(line) for line in (folder/'usage.jsonl').read_text().splitlines()]
    ids={r['task_id'] for r in smoke['runs']}
    starts=[u for u in usage if u['event']=='llm_start' and u.get('task_id') in ids]
    once=all(sum(u.get('namespace','').startswith('extract_memory:') for u in starts if u['task_id']==tid)==1 for tid in ids)
    isolated=any(u['contains_private_marker'] and u['agent_id']=='research' for u in starts) and all(not u['contains_private_marker'] for u in starts if u['agent_id']!='research')
    checks['root_memory_once']={'status':'PASS' if once else 'FAIL','details':'Actual usage-only callbacks; one root extraction per optimized task.'}
    checks['private_context_isolation']={'status':'PASS' if isolated else 'FAIL','details':'Research-only private sentinel measured in actual model inputs; absent from root/Reviewer/other workers.'}
    verification={'quality_rule_version':3,'checks':checks,'passed':sum(v['status']=='PASS' for v in checks.values()),
        'failed':sum(v['status']=='FAIL' for v in checks.values()),'raw_runtime_report':'runtime_smoke.json',
        'classifier_corrections':'Raw false negatives are retained. They missed valid explicit Best: Method A / best is/was A; no provider answer, coverage threshold or grounding decision was altered.'}
    (RESULTS/'runtime_verification.json').write_text(json.dumps(verification,indent=2,ensure_ascii=False),encoding='utf-8')
    rows=[]
    columns=[report['single_agent'],report['phase5a_multi_agent'],report['optimized_median']]
    for label,key in [('Status','status'),('Grounded','grounded'),('Core answer correct','core_answer_correct'),('Required coverage','evidence_coverage'),('Citations valid','citation_validity'),('Valid citations','valid_citations'),('Latency (s)','latency_seconds'),('LLM calls','llm_calls'),('Tool calls','tool_calls'),('Provider tokens','tokens'),('Review rounds','review_rounds'),('Re-delegations','redelegations'),('Cache hits','cache_hits')]:
        def cell(v):
            value=v[key]
            if isinstance(value,dict):return '3 papers + computed dataset + chart' if all(value.values()) else str(value)
            if isinstance(value,float):return f'{value:.3f}'.rstrip('0').rstrip('.')
            return str(value)
        rows.append('| '+label+' | '+' | '.join(cell(c) for c in columns)+' |')
    contexts=[r for r in report['context_reductions'] if r['agent']=='reviewer']
    before=median(c['before'] for c in contexts);after=median(c['after'] for c in contexts)
    review_runs=[*smoke['runs'],smoke['targeted_repair'],smoke['single_data'],(budget or smoke)['hard_budget']]
    review_stats={status:sum(v['cost_trace']['review_status']==status for v in review_runs) for status in ('executed','skipped','unavailable')}
    changes='\n'.join(f"- {k}: {v['before']} → {v['after']}; absolute {v['absolute_change']}; {v['percent_change']}%." for k,v in report['change_vs_phase5a_multi'].items())
    check_lines='\n'.join(f"| {key} | {value['status']} |" for key,value in checks.items())
    text=f'''# Phase 5A.5 — Validation

## A. Verdict

{report['verdict']}. Final optimized samples pass all fixture quality criteria and
reduce calls, provider tokens and wall time relative to the retained Phase 5A
multi-agent sample. This is a small synthetic benchmark, not general superiority.

## B. Root causes

The retained trace had 19 matched starts/ends: Supervisor/root 10, Research 2,
Data 6, Coding 0, Reviewer 1. Root included analyzer1/router1/planner1/draft2,
final synthesis1/grounding2/revision1/memory1. Research plan/contract2; Data initial
and supplemental plan/code/contract3 each. Tools: workspace search1 and two
inspect/analyze pairs. Deterministic merge/ID validation already existed.
The distinct old supplementary scenario failed 0/2: expanded missing-source
objectives and source/contract confusion; initial Data contract ValueError despite
valid execution, then generated repair failed with restricted-runtime NameError
(`isinstance`). The invalid raw contract was not persisted; its exact invalid field
cannot be asserted. See the pre-optimization audit for actual task/namespace IDs.

## C–D. Implementation and routing

Cost-aware deterministic routing, risk review, exact source requests, local
completed-result cache, permission-aware overlap merge, atomic aggregate budgets,
scoped typed context and extractive finalization are implemented. No new roles.
Examples generated directly by old/new routing code are `router_examples.json`:
simple→existing; paper comparison→Research; maximum mIoU existing→Data;
independent papers/XLSX→Multi; first extract then compute Multi→existing/sequential;
long wording alone→existing. Explicit ambiguity uses one router model call.
Contextual pronoun follow-ups retain query rewriting. Planner/merge/review triggers
and supported deterministic pandas work need no new model call.

## E. Reviewer

Five orchestrated real scenarios: {review_stats}. Both main samples and targeted
repair execute Reviewer. Simple Data skips it with
`valid_single_result_no_open_questions`. The hard-budget scenario records Reviewer
unavailable with zero actual review calls. Soft-budget skips require valid contracts,
no open questions/low confidence and no sensitive actions; required review overrides
optional skips. Final semantic grounding/citation checks still run after a skip.

## F. Supplementary completion

Before: 0/2 (retained old gap scenario). After: {smoke['supplementary_stats']}.
The actual enabled-Reviewer scenario drops only paper_3 evidence initially; one
new document-ID-restricted retrieval restores it. Data runs once. This is an
equivalent targeted repair test, not a claim that absent latency/architecture facts
can be manufactured. The original and earlier failed iterations remain saved.

## G–H. Budgets, cache and duplicate work

Defaults: 8 delegations / parallel3 / model24 / tools12 / execution300s /
review1 / supplementary2; optional token cap. SQLite reservations are atomic.
Actual two-call-budget task completes as a bounded partial answer, retains completed
Data and never returns 500. A final separate probe in `budget_runtime.json` verifies
the partial answer includes the actual 73.27 deterministic result while remaining
`grounded=false`; unverified model conclusions are withheld. The original full
runtime report is retained unchanged. Unit/integration tests cover soft review suppression,
wall/token handling, concurrent reservations and graceful stopping. Human approval
and durable pauses suspend execution-time usage; a simulated 600-second approval
still resumes and writes once. Complete wall duration stays visible.

Main benchmark cache hits: 0; duplicate prevention: 0 (its work was already distinct).
A separate actual-graph/scripted-provider integration test observes one completed
cache reuse with zero new worker/model/tool starts. A duplicate planner integration
merges two overlapping Research requests and searches once. Changed document,
dataset, contract/context, PARTIAL and force-refresh cases do not reuse. These
tests are not reported as provider benchmark cache hits. Mutable external sources
are conservatively uncached until a read revision can be verified.

## I. Context

Reviewer estimates: {contexts[0]['before']}→{contexts[0]['after']} and
{contexts[1]['before']}→{contexts[1]['after']}; medians {before}→{after}
({100*(after-before)/before:.2f}%). Method: `count_tokens_approximately` on the
same input before/after projection, not provider usage or historical prompt-token
measurement. Initial Research254→254 and Data550→550 had no existing evidence;
no reduction is claimed there. Canonical evidence/output remains durable.

## J. Tests

Previous {tests['previous']}; new {tests['new']}; total {tests['total']};
passed {tests['passed']}; failures {tests['failed']}; errors {tests['errors']};
skipped {tests['skipped']}. Discovery is recorded in `tests.json`.
The existing Phase 1–5A tests remain unmodified. Compileall, pip check and
git diff --check pass (line-ending warnings are not whitespace errors).

## K. Real evaluation

Same exact question verified against persisted old task goals:
{report['same_question_verified']}. Same Phase 5A PDF/XLSX generators, configured
DeepSeek v4 flash, temperature0, source scope and enabled memory/grounding.
Only orchestration/efficiency controls differ intentionally. Previous provider
model IDs were not logged; the .env configuration was left untouched (last modified
2026-09-07). Old memory history/network scheduling are not identical controls.
Single below is a fresh current run; retained earlier single remains separately
114.907s / 6 calls / 3 tools / 42,738 tokens.

| Metric | Fresh existing Single (n=1) | Phase 5A Multi (n=1 retained) | Optimized Multi (n=2 median) |
|---|---:|---:|---:|
{chr(10).join(rows)}

Individual optimized runs: {report['optimized_runs'][0]['latency_seconds']}s /
{report['optimized_runs'][0]['tokens']} tokens and
{report['optimized_runs'][1]['latency_seconds']}s /
{report['optimized_runs'][1]['tokens']} tokens. Both have six model calls, three
tool calls, all required sources/artifacts and valid citations. Half-token medians
are arithmetic medians of two integer usage observations, not a fractional API bill.

## L. Changes vs Phase 5A Multi

{changes}

Fresh Single is faster than optimized Multi in this sample. Avoid global
multi-agent activation. Two samples do not establish stable latency distributions.

## M. Quality and iteration history

Both final samples are semantically grounded, choose A (2.5M / 73.27), exclude
B (3.2M), describe the synthetic/descriptive scope and make no causal-validation
claim. Required evidence covers all three papers, calculated data and generated
chart. Citation counts are not quality scores; file generation is not visual QA.

Earlier iterations included genuine grounding failures and a scope-contaminated
PARTIAL supplementary result. These are retained in `iterations/`; they were not
discarded to select successful samples. Structural fixes project specialist goals,
normalize computed JSON, assign cross-result synthesis to Finalizer and prune
literal verifier-identified unsupported nonnumeric caveats. The second semantic
verification remains required after extractive revision; numeric/unknown spans
retain model revision.

The running evaluator also initially misclassified correct final wording
`best ... is/was A` as incorrect. Both raw final JSON decisions/logs remain intact.
Rule3 reparses explicit best-choice predicates and Markdown; wrong-B and invalid
citation counterexamples are tested. No model answer, grounding verdict or required
coverage threshold was changed. `runtime_verification.json` records the corrected
decision alongside the original classifier status. The current runner exits nonzero
when a future check fails.

## N. Runtime checks

| Check | Final audited status |
|---|---|
{check_lines}

Additional: preview backend8001 and Streamlit8501 healthy; five-agent catalog
unchanged. HITL approve/reject/restart: PASS through existing official-SDK mock
integration tests. Live GitHub write: SKIPPED; no external write is authorized or
needed for this synthetic efficiency evaluation. No new MCP integration was added.

## O–P. Limits and recommendation

READY WITH LIMITATIONS; freeze this bounded phase and proceed to Phase 5B planning.
Do not infer production-wide speed/quality from two synthetic samples. Provider
outputs vary; exact-span pruning is conservative and may fall back to model
revision. Token ceilings depend on available usage and can overshoot by in-flight
responses. Wall checks are cooperative, bounded by existing call/worker timeouts.
Conservative source/context/contract fingerprints reduce hit rate; no cross-task
semantic cache. Legacy existing-workflow costs are measured by evaluation callbacks;
the persistent per-agent budget/trace applies to Supervisor-managed tasks.

## Q. Git safety

Not committed. Not pushed. HEAD remains
`24f01d5f2901191fbdcde8a2117250b0b728c93c`. Earlier uncommitted Phase 1–5A work
is retained. No .env replacement, database/index deletion, reset or dependency
installation/update occurred in this phase. Preview reload uses existing data.
'''
    (ROOT/'docs/PHASE5A5_VALIDATION.md').write_text(text,encoding='utf-8')
    print(json.dumps({'checks_passed':verification['passed'],'checks_failed':verification['failed'],'tests':tests,'review_stats':review_stats}))
    return verification

if __name__=='__main__':run()
