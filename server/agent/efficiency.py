"""Deterministic cost decisions, scoped context and task-local execution identities."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from server.tasks import TaskConflict


class BudgetExhausted(TaskConflict):
    pass


class MultiAgentBudget(BaseModel):
    max_delegations: int = Field(default=8, ge=1)
    max_parallel: int = Field(default=3, ge=1)
    max_llm_calls: int = Field(default=24, ge=1)
    max_tool_calls: int = Field(default=12, ge=1)
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_wall_time_seconds: float = Field(default=300, gt=0)
    max_review_rounds: int = Field(default=1, ge=0, le=1)
    max_redelegations: int = Field(default=2, ge=0, le=2)


class SupplementaryDelegationRequest(BaseModel):
    exact_missing_items: list[str] = Field(min_length=1)
    target_sources: list[str]
    existing_evidence_ids: list[str]
    do_not_repeat: list[str]
    expected_fields: list[str]


class BudgetManager:
    """State lives in the supervisor ledger, reservations use its SQLite transaction."""
    FINALIZER_PHASES = {'final_synthesis', 'grounding', 'answer_revision', 'memory'}

    @staticmethod
    def initial(config):
        fields = {k: config.get('MULTI_AGENT_' + k.upper(), default) for k, default in
            MultiAgentBudget().model_dump().items()}
        fields['max_wall_time_seconds'] = config.get('MULTI_AGENT_MAX_WALL_TIME_SECONDS', 300)
        fields['max_review_rounds'] = config.get('MULTI_AGENT_REVIEW_MAX_ROUNDS', 1)
        fields['max_redelegations'] = config.get('MULTI_AGENT_REDELEGATION_MAX', 2)
        return {'limits': MultiAgentBudget(**fields).model_dump(), 'started_at': datetime.now(timezone.utc).isoformat(),
            'llm_calls_used': 0, 'tool_calls_used': 0, 'tokens_used': None, 'known_token_subtotal': 0,
            'token_samples': 0, 'delegations_used': 0, 'redelegations_used': 0, 'review_rounds_used': 0,
            'budget_exhausted': False, 'exhausted_reasons': [], 'phase_calls': {}}

    @staticmethod
    def snapshot(state, ratio=.8):
        result = dict(state)
        ended = datetime.fromisoformat(state['completed_at']) if state.get('completed_at') else datetime.now(timezone.utc)
        paused = state.get('paused_seconds',0)
        if state.get('paused_at'):
            paused += max(0,(ended-datetime.fromisoformat(state['paused_at'])).total_seconds())
        result['elapsed_seconds'] = max(0, (ended-datetime.fromisoformat(state['started_at'])).total_seconds()-paused)
        limits = state['limits']
        ratios = [state['llm_calls_used']/limits['max_llm_calls'], state['tool_calls_used']/limits['max_tool_calls'],
            state['delegations_used']/limits['max_delegations'], result['elapsed_seconds']/limits['max_wall_time_seconds']]
        if limits['max_total_tokens'] and state['tokens_used'] is not None:
            ratios.append(state['tokens_used']/limits['max_total_tokens'])
        result['utilization'] = max(ratios)
        result['soft_reached'] = result['utilization'] >= ratio
        result['token_usage_status'] = 'available' if state['tokens_used'] is not None else 'unavailable'
        return result

    @staticmethod
    def pause(state):
        state.setdefault('paused_at',datetime.now(timezone.utc).isoformat())

    @staticmethod
    def resume(state):
        paused=state.pop('paused_at',None)
        if paused:
            state['paused_seconds']=state.get('paused_seconds',0)+max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(paused)).total_seconds())

    @classmethod
    def reserve(cls, state, kind, phase='model'):
        used = kind + '_used'
        limits = state['limits']
        snapshot = cls.snapshot(state)
        reason = None
        finalizer = phase in cls.FINALIZER_PHASES
        if state['budget_exhausted'] and not finalizer:
            reason = state['exhausted_reasons'][0]
        elif kind == 'llm_calls' and state[used] >= limits['max_llm_calls'] - (0 if finalizer else min(4, max(0,limits['max_llm_calls']-1))):
            reason = 'llm_calls'
        elif kind == 'tool_calls' and state[used] >= limits['max_tool_calls']:
            reason = 'tool_calls'
        elif kind == 'delegations' and state[used] >= limits['max_delegations']:
            reason = 'delegations'
        elif kind == 'delegations' and phase=='reviewer' and state['review_rounds_used'] >= limits['max_review_rounds']:
            reason = 'review_rounds'
        elif kind == 'delegations' and phase=='supplementary' and state['redelegations_used'] >= limits['max_redelegations']:
            reason = 'redelegations'
        elif snapshot['elapsed_seconds'] >= limits['max_wall_time_seconds']:
            reason = 'wall_time'
        elif limits['max_total_tokens'] and state['known_token_subtotal'] >= limits['max_total_tokens']:
            reason = 'tokens'
        if reason:
            state['budget_exhausted'] = True
            state['exhausted_reasons'] = list(dict.fromkeys([*state['exhausted_reasons'], reason]))
            return reason
        state[used] += 1
        if kind=='delegations' and phase=='reviewer':state['review_rounds_used'] += 1
        if kind=='delegations' and phase=='supplementary':state['redelegations_used'] += 1
        if kind == 'llm_calls':
            state['phase_calls'][phase] = state['phase_calls'].get(phase, 0) + 1
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def normalized_objective(text):
    return re.sub(r'\s+', ' ', text.strip().casefold())


def cache_key(request, scope):
    context = request.context.model_dump(exclude={'notes'})
    return digest({'task_id': request.task_id or request.supervisor_run_id, 'agent_id': request.agent_id,
        'objective': normalized_objective(request.objective), 'context': context,
        'sources': scope.get('source_versions'), 'contract': request.expected_output,
        'schema_version': 2, 'tools': scope.get('tool_snapshot'), 'allowed': request.allowed_tool_capabilities,
        'budget': [request.max_iterations, request.max_tool_calls], 'supplementary': request.supplementary})


def cost_decision(candidate, goal):
    # Extracting source values and subsequently computing with THEM is dependent.
    sequential = bool(re.search(r'(?:first|先).{0,100}(?:extract|提取).{0,100}(?:then|再|然后).{0,80}(?:calculat|comput|计算)', goal, re.I))
    count = len(candidate.required_capabilities)
    return candidate.model_copy(update={'mode': 'existing_workflow' if sequential else candidate.mode,
        'independent_subtasks': 0 if sequential else count, 'dependency_type': 'sequential' if sequential else 'parallel' if count>1 else 'none',
        'estimated_agent_count': 0 if sequential else count, 'expected_multi_agent_value': 'high' if count>1 and not sequential else 'low',
        'estimated_cost_level': 'medium' if count>1 else 'low',
        'reason_summary': 'Extracted source values are an input to later computation; use one sequential workflow.' if sequential else
            'Distinct required source/tool boundaries can run independently.' if count>1 else 'One specialty suffices; avoid cross-specialty orchestration.'})


def ambiguous(goal, candidate):
    return candidate.mode == 'existing_workflow' and bool(re.search(r'check consistency|investigate|evaluate the project|分析项目|综合评估', goal, re.I))


def overlap_specs(specs):
    """Conservative same-capability/target/intent overlap; never cross permissions."""
    result, removed = [], []
    def tokens(text):
        text = re.sub(r'\b(analyze|compare|inspect|examine|review)\b', 'inspect', text.casefold())
        return set(re.findall(r'[\w.]+', text)) - {'the', 'in', 'of', 'and', 'same', 'these'}
    def targets(text):
        return set(re.findall(r'[\w./-]+\.(?:pdf|csv|xlsx|py|md)\b|https?://\S+|\bpaper\s+[a-z0-9]\b|\bsheet\s*\w+\b',text.casefold()))
    for spec in specs:
        a = tokens(spec.objective)
        duplicate = next((r for r in result if r.capability==spec.capability and targets(r.objective)==targets(spec.objective) and
            (normalized_objective(r.objective)==normalized_objective(spec.objective) or
             len(a & tokens(r.objective))/max(1,len(a | tokens(r.objective))) >= .85) and r.depends_on==spec.depends_on), None)
        if duplicate:
            removed.append((spec.key, duplicate.key))
        else:
            result.append(spec)
    return result, removed


class AgentContextBuilder:
    """Select by identity, source and objective; canonical evidence remains untouched."""
    def build(self, request, scope):
        scope = dict(scope)
        terms = set(re.findall(r'\w+', request.objective.casefold()))
        targets = set((request.supplementary or {}).get('target_sources', []))
        allowed_sources = {'research':{'workspace','pdf','web','arxiv'}, 'data':{'analysis'}, 'coding':{'github','mcp'}}.get(request.agent_id)
        evidence = [e for e in scope.get('selected_evidence', []) if allowed_sources is None or e.get('source_type') in allowed_sources]
        def score(e):
            target = e.get('document_id') in targets or e.get('document_name') in targets
            return 1000*target + len(terms & set(re.findall(r'\w+', e.get('content','').casefold())))
        selected = sorted(evidence, key=score, reverse=True)[:4]
        scope['selected_evidence'] = [{k:e[k] for k in ('evidence_id','source_type','document_id','document_name','page','execution_id','source_metadata') if k in e}
            | {'content': e.get('content','')[:700]} for e in selected]
        scope['selected_memory'] = scope.get('selected_memory', [])[:2]
        scope['dataset_metadata'] = [{k:a[k] for k in ('id','filename','schema') if k in a} for a in scope.get('dataset_metadata', [])]
        return scope


def gap_satisfied(item, context, evidence):
    terms = set(re.findall(r'\w+', item.casefold())) - {'the','a','of','for','in','missing','evidence'}
    ids = {e['evidence_id'] for e in evidence}
    for fact in context.get('known_facts', []):
        if not fact.get('evidence_ids') or set(fact['evidence_ids'])-ids:
            continue
        words = set(re.findall(r'\w+', fact['claim'].casefold()))
        # Strict lexical match; no inferred semantic coverage or naked ID match.
        if terms and terms <= words:
            return True
    return False


def finalizer_evidence(evidence, limit=2400):
    """Decode successful analysis JSON before projection; preserve computed values verbatim."""
    projected=[]
    for item in evidence[:8]:
        content=item.get('content','')
        if item.get('source_type')=='analysis':
            try:
                value=json.loads(content)
                if 'computed_output' in value:
                    output=json.loads(value['computed_output'])
                    value={'computed_output':output,'dataset_context':[{k:a[k] for k in ('dataset_id','filename') if k in a} for a in value.get('dataset_context',[])]}
                    content=json.dumps(value,ensure_ascii=False)
            except (ValueError,TypeError):
                pass
        projected.append({k:item[k] for k in ('evidence_id','source_type','document_name','page','execution_id','source_metadata') if k in item} | {'content':content[:limit]})
    return projected
