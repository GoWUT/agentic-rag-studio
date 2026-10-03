"""Selective efficiency policies on the existing persistent supervisor graph."""
import json
import re
from types import SimpleNamespace
from server.agent.orchestration import OrchestrationNodes, routing_candidate, deterministic_review, synthesis_context
from server.agent.orchestration_schemas import (OrchestrationDecision, DelegationSpec, SupervisorPlan,
    SharedTaskContext, AgentRegistrySnapshot, DelegationRequest, DelegationResult)
from server.agent.schemas import QueryAnalysis, VerificationResult
from server.agent.nodes import _latest_user_query
from server.agent.efficiency import (BudgetManager, BudgetExhausted, AgentContextBuilder, cache_key,
    cost_decision, ambiguous, overlap_specs, gap_satisfied, SupplementaryDelegationRequest)
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from contextvars import ContextVar
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.errors import GraphInterrupt


def efficient_decision(goal,has_data):
    candidate=routing_candidate(goal,has_data)
    if has_data and not candidate.required_capabilities and re.search(r'\b(?:statistics|how many rows|row count|sort|mean|average|minimum|maximum)\b|排序|平均|多少行',goal,re.I):
        candidate=OrchestrationDecision(mode='single_agent',required_capabilities=['data.analyze'],reason_summary='Uploaded data supports one statistical task.')
    return cost_decision(candidate,goal)


class EfficientOrchestrationNodes(OrchestrationNodes):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.analysis_shortcut = ContextVar('efficiency_query_analysis',default=None)

    def query_analyzer(self, state):
        shortcut = None
        query = _latest_user_query(state['messages'])
        assets = self.services.data.list(state['workspace_id']) if state.get('workspace_id') else []
        decision = efficient_decision(query, bool(assets))
        followup = len(state['messages'])>1 and bool(re.match(r'(?:what|how|why|does|is|and)\b.{0,45}\b(?:it|they|that|those|this|these)\b|它|那|这些',query,re.I))
        if self.config.get('MULTI_AGENT_ENABLED', False) and self.config.get('MULTI_AGENT_COST_AWARE_ROUTING', True) and not followup and not state.get('task_resume'):
            shortcut = self._fallback_analysis(query)
        token = self.analysis_shortcut.set(shortcut)
        if shortcut is not None:
            self.context_harness.prepare([HumanMessage(content=query)])
        try:
            return super().query_analyzer(state)
        finally:
            self.analysis_shortcut.reset(token)

    def _invoke_structured(self, schema, messages):
        if schema is QueryAnalysis and self.analysis_shortcut.get() is not None:
            return self.analysis_shortcut.get()
        if schema not in self._structured_models:
            self._structured_models[schema] = self.llm.with_structured_output(schema,method='json_mode',include_raw=True)
        if schema is VerificationResult and self.root_budget_state.get():
            messages=[SystemMessage(content=str(messages[0].content)+' For unsupported claims also populate unsupported_sentences with exact verbatim complete sentences copied from the candidate answer. These are removable text spans, not explanations. A missing detail in a summary does not establish its absence from the source.'),*messages[1:]]
        return super()._invoke_structured(schema, messages)

    def orchestration_router(self, state):
        if (state.get('task_id') and self.phase5.store.supervisor(state['task_id'])) or not self.config.get('MULTI_AGENT_ENABLED',False) or state.get('task_resume') or state.get('task_boundary_stop'):
            return super().orchestration_router(state)
        if not self.config.get('MULTI_AGENT_COST_AWARE_ROUTING',True):
            return super().orchestration_router(state)
        goal = state['original_query']
        decision = efficient_decision(goal, bool(state.get('data_asset_ids')))
        pre_calls = state.get('pre_orchestration_llm_calls',0)
        analyzer_calls=pre_calls
        usage = list(state.get('pre_orchestration_usage',[]))
        if ambiguous(goal,decision):
            # The only extra router model call is this ambiguous fallback.
            token = self.pre_calls.set(0); usage_token = self.pre_usage.set([])
            try:
                proposed = self._invoke_structured(OrchestrationDecision,[SystemMessage(content='Choose required capabilities from catalog only. Multi-agent needs distinct necessary independent specialties. Long wording alone has low value. Use existing_workflow when unclear. Brief reason only.'),
                    HumanMessage(content=json.dumps({'goal':goal,'has_dataset':bool(state.get('data_asset_ids')),
                        'catalog':[a.model_dump(include={'agent_id','capabilities'}) for a in self.phase5.registry.list_agents()]}))])
                if 'data.analyze' in proposed.required_capabilities and not state.get('data_asset_ids'):
                    proposed = decision
                selected = {self.phase5.registry.find_by_capability(c)[0].agent_id for c in proposed.required_capabilities}
                if selected & {'supervisor','reviewer'}:
                    proposed = decision
                if len(selected)<2 and proposed.mode=='multi_agent':
                    proposed.mode = 'single_agent' if selected else 'existing_workflow'
                decision = cost_decision(proposed,goal)
            except Exception:
                pass
            finally:
                pre_calls += self.pre_calls.get(); usage += self.pre_usage.get()
                self.pre_calls.reset(token); self.pre_usage.reset(usage_token)
        if decision.mode=='existing_workflow':
            self.phase5.event(state.get('task_id'),'ORCHESTRATION_ROUTED',decision.model_dump())
            return {'orchestration_decision':decision.model_dump()}
        snapshot = self.phase5.registry.snapshot()
        self.phase5.registry.get('supervisor',snapshot)
        decision.suggested_agents = list(dict.fromkeys(self.phase5.registry.find_by_capability(c,snapshot)[0].agent_id for c in decision.required_capabilities))
        supervisor = self.phase5.store.create_supervisor(state.get('task_id'),goal,decision.mode,
            {'decision':decision.model_dump(),'agent_snapshot':snapshot.model_dump(),'review_round':0,'redelegation_count':0})
        self.phase5.store.record_pre_orchestration_calls(supervisor['id'],analyzer_calls,pre_calls-analyzer_calls)
        for sample in usage:
            self.phase5.store.add_usage(supervisor['id'],sample)
        return {'orchestration_decision':decision.model_dump(),'supervisor_run_id':supervisor['id'],
            'agent_snapshot':snapshot.model_dump(),'orchestration_group':0,'review_round':0,'redelegation_count':0}

    def plan_model(self, state):
        model = super().plan_model(state)
        goal = state['original_query']
        clear = bool(re.search(r'\.(csv|xlsx)|three uploaded papers|\b3 papers\b|三篇',goal,re.I))
        write = bool(re.search(r'create.{0,45}issue|add.{0,45}comment|创建|添加评论',goal,re.I))
        def invoke(messages,schema):
            if clear and not write:
                objectives = {'research.compare':'Collect only reported method descriptions and numerical experimental results from the requested papers/documents. State source limitations relevant to the goal; do not demand unreported architecture or training facts. No dataset computations.',
                    'data.analyze':'Analyze only the uploaded dataset: '+goal+'. Do not perform new research or interpret paper claims.',
                    'code.review':'Inspect only requested repository/code paths: '+goal}
                specs = [DelegationSpec(key='work_'+str(i),capability=c,objective=objectives.get(c,goal)[:3000]) for i,c in enumerate(state['orchestration_decision']['required_capabilities'])]
                plan = SupervisorPlan(goal=goal,delegations=specs,execution_groups=[[s.key for s in specs]],reviewer_required=len(specs)>1)
            else:
                plan = model.invoke(messages,schema)
            if self.config.get('MULTI_AGENT_DUPLICATE_DETECTION',True):
                specs, removed = overlap_specs(plan.delegations)
                mapping = dict(removed)
                plan.delegations = specs
                plan.execution_groups = [list(dict.fromkeys(mapping.get(key,key) for key in group)) for group in plan.execution_groups]
                for spec in plan.delegations:
                    spec.depends_on = list(dict.fromkeys(mapping.get(key,key) for key in spec.depends_on))
                self.phase5.store.update_supervisor(state['supervisor_run_id'],duplicate_delegations_prevented=len(removed))
            return plan
        return SimpleNamespace(invoke=invoke)

    def request(self,state,spec,group,revision_of=None,supplementary=None):
        run=self.phase5.store.run(state['supervisor_run_id'])
        budget=run['metadata'].get('budget')
        if budget and (budget['budget_exhausted'] or budget['delegations_used']>=budget['limits']['max_delegations']):
            budget['budget_exhausted']=True
            budget['exhausted_reasons']=list(dict.fromkeys([*budget['exhausted_reasons'],'delegations']))
            self.phase5.store.update_supervisor(run['id'],budget=budget)
            raise BudgetExhausted('Multi-agent delegation budget exhausted')
        return super().request(state,spec,group,revision_of,supplementary)

    def context_for(self,state,request,context):
        scope = super().context_for(state,request,context)
        def estimate(value):
            brief={'objective':request.objective,'context':request.context.model_dump(),
                'selected_memory':value.get('selected_memory',[]),'selected_evidence':value.get('selected_evidence',[]),
                'dataset_metadata':value.get('dataset_metadata',[])}
            return count_tokens_approximately([HumanMessage(content=json.dumps(brief,ensure_ascii=False))])
        before=estimate(scope)
        scope['selected_evidence'] = [e for e in state.get('evidence',[]) if e['evidence_id'] in request.context.evidence_ids]
        documents = self.phase5.manager.workspaces.documents(state['workspace_id']) if state.get('workspace_id') else []
        scope['source_versions'] = {'documents':sorted((d.id,d.fingerprint,d.index_id) for d in documents),
            'datasets':sorted((a.id,a.fingerprint) for a in self.services.data.list(state['workspace_id'])) if state.get('workspace_id') else []}
        scope['document_sources'] = [{'id':d.id,'filename':d.display_name} for d in documents]
        # Mutable external sources without an explicit commit cannot be safely reused.
        sha = re.search(r'\b[0-9a-f]{40}\b',request.objective)
        # A SHA mentioned in a request does not prove the transport read that revision.
        scope['cacheable_sources'] = request.context.source_scope=='workspace_only'
        if sha:
            scope['source_versions']['github_sha'] = sha[0]
        scope['supplementary'] = request.supplementary
        scope=AgentContextBuilder().build(request,scope)
        record=self.phase5.store.delegation(request.delegation_id)
        self.phase5.store.annotate_run(record['agent_run_id'],context_reduction={'before':before,'after':estimate(scope),
            'method':'Same private brief projected by Phase5A vs AgentContextBuilder; count_tokens_approximately estimate'})
        return scope

    def agent_worker(self,state,config):
        request = DelegationRequest.model_validate(state['delegation_request'])
        key = cache_key(request,state['worker_scope'])
        record = self.phase5.store.delegation(request.delegation_id)
        self.phase5.registry.validate(request,AgentRegistrySnapshot.model_validate(state['agent_snapshot']),config=self.config,
            cancelled=bool(request.task_id and self.services.tasks.get(request.task_id).status=='CANCELLED'))
        if record['result_json']:
            return super().agent_worker(state,config)
        if self.config.get('MULTI_AGENT_DELEGATION_CACHE',True) and state['worker_scope'].get('cacheable_sources') and not request.force_refresh:
            cached = self.phase5.store.cached(request.task_id,request.supervisor_run_id,key)
            if cached:
                oldrun = self.phase5.store.run(cached['agent_run_id'])
                self.phase5.store.progress(record['agent_run_id'],**oldrun['metadata']['progress'])
                outcome = dict(cached['result_json'],delegation_id=request.delegation_id,revision_of=request.revision_of)
                outcome['payload'] = dict(outcome['payload'],delegation_id=request.delegation_id)
                self.phase5.store.annotate_run(record['agent_run_id'],cache_key=key,cache_hit=True,reused_delegation_id=cached['id'])
                self.phase5.store.finish(request.delegation_id,outcome)
                self.phase5.event(request.task_id,'DELEGATION_CACHE_HIT',{'delegation_id':request.delegation_id,'reused':cached['id']})
                return {'agent_outcomes':[outcome]}
        self.phase5.store.annotate_run(record['agent_run_id'],cache_key=key,cache_hit=False)
        return super().agent_worker(state,config)

    def draft_synthesis(self,state):
        # Reviewer reads typed summaries; a second narrative draft adds no new evidence.
        return {'orchestration_draft':json.dumps(synthesis_context(state['shared_context']),ensure_ascii=False)[:2500]}

    def answer_revision(self,state):
        verdict=state.get('verification_result') or {}
        spans=verdict.get('unsupported_sentences') or []
        answer=state.get('final_answer','')
        if spans and len(spans)<=4 and all(0<len(span)<=500 and span in answer and not re.search(r'\d',re.sub(r'\[ev_[^\]]+\]','',span)) and re.search(r'\bno\b|\bnot\b|cannot|missing|unavailable|unable|not reported',span,re.I) for span in spans):
            for span in spans:answer=answer.replace(span,'')
            answer=re.sub(r'(?m)^\s*[-*]\s*$','',answer).strip()
            if answer:
                self.phase5.store.update_supervisor(state['supervisor_run_id'],deterministic_revision={'removed_sentences':len(spans),'reason':'Exact verifier-identified unsupported caveats; numeric statements retained; semantic recheck required.'})
                return {'final_answer':answer,'revision_count':state.get('revision_count',0)+1}
        return super().answer_revision(state)

    def supervisor_actions(self,state):
        try:
            return super().supervisor_actions(state)
        except GraphInterrupt:
            self.phase5.store.pause_budget(state['supervisor_run_id'])
            raise
        except BudgetExhausted:
            shared = SharedTaskContext.model_validate(state['shared_context'])
            shared.open_questions.append('Budget stopped remaining requested external actions; no success is claimed.')
            return {'action_results':self.phase5.store.run(state['supervisor_run_id'])['metadata'].get('action_results',[]),
                'shared_context':shared.model_dump()}

    def citation_validator(self,state):
        updates=super().citation_validator(state)
        root=self.phase5.store.run(state['supervisor_run_id']) if state.get('supervisor_run_id') else None
        budget=root['metadata'].get('budget') if root else None
        if not budget or not budget['budget_exhausted'] or (state.get('verification_result') or {}).get('grounded'):
            return updates
        # Never publish an unverified model draft; deterministic execution facts remain useful.
        lines=['Budget exhausted. This is a partial execution report; the complete answer has not passed semantic verification.']
        evidence_ids={e['evidence_id'] for e in state.get('evidence',[])}
        for record in self.phase5.store.delegations(root['id']):
            if record['agent_id']=='reviewer':continue
            run=self.phase5.store.run(record['agent_run_id'])
            result=record.get('result_json') or {}
            lines.append(record['agent_id'].title()+': '+record['status']+'; '+str(len(result.get('evidence_ids',[])))+' collected evidence records.')
            calculated=run['metadata'].get('progress',{}).get('structured_analysis')
            if record['agent_id']=='data' and record['status']=='COMPLETED' and calculated:
                eid='ev_'+calculated['execution_id'].replace('-','')
                if eid in evidence_ids:
                    lines.append('Completed deterministic calculation: '+json.dumps(calculated['metrics'],ensure_ascii=False)[:1800]+' ['+eid+']')
        gaps=state.get('shared_context',{}).get('open_questions',[])
        lines.append('Unresolved: full synthesis/grounding and any remaining requirements. Budget reasons: '+', '.join(budget['exhausted_reasons']))
        lines.extend(q[:250] for q in gaps[:4])
        from server.agent.evidence import Evidence, render_citations
        answer,citations=render_citations(self.phase4.policy.secrets.clean('\n\n'.join(lines)),[Evidence.model_validate(e) for e in state.get('evidence',[])],state.get('document_registry'))
        updates.update(final_answer=answer,messages=[AIMessage(content=answer,id=updates['messages'][-1].id)],citations=[c.model_dump() for c in citations])
        updates['trace_summary']['citation_count']=len(citations)
        if state.get('task_id'):
            self.services.tasks.checkpoint(state['task_id'],1,{**{k:v for k,v in updates.items() if k!='messages'},
                'verification_result':state.get('verification_result'),'evidence':state.get('evidence',[]),'artifacts':state.get('artifacts',[])})
        return updates

    def review_agents(self,state,config):
        if self.phase5.store.run(state['supervisor_run_id'])['metadata'].get('review_result'):
            return super().review_agents(state,config)
        context = SharedTaskContext.model_validate(state['shared_context'])
        check = deterministic_review(context,state['evidence'],state.get('artifacts',[]))
        policy = self.config.get('MULTI_AGENT_REVIEW_POLICY','risk_based')
        results = context.delegation_results
        risk = len({r.agent_id for r in results})>1 or check.verdict!='pass' or any(r.unresolved_questions or r.confidence<.7 for r in results) or bool(state['supervisor_plan'].get('actions'))
        reason = None
        if not self.config.get('MULTI_AGENT_REVIEW_ENABLED',True) or policy=='disabled': reason='policy_disabled'
        elif policy=='multi_agent_only' and len({r.agent_id for r in results})<2: reason='single_specialty'
        elif policy=='risk_based' and not risk: reason='valid_single_result_no_open_questions'
        budget = self.phase5.store.run(state['supervisor_run_id'])['metadata'].get('budget')
        if budget and BudgetManager.snapshot(budget,self.config.get('MULTI_AGENT_SOFT_BUDGET_RATIO',.8))['soft_reached'] and check.verdict=='pass' and all(not r.unresolved_questions and r.confidence>=.7 for r in results) and not state['supervisor_plan'].get('actions'):
            reason='soft_budget_optional_review'
        if reason and not self.config.get('MULTI_AGENT_REVIEW_REQUIRED',False):
            self.phase5.store.update_supervisor(state['supervisor_run_id'],review_status='skipped',review_skip_reason=reason)
            return {'review_result':None}
        self.phase5.store.update_supervisor(state['supervisor_run_id'],review_status='executed',review_skip_reason=None)
        state = {**state,'supervisor_plan':{**state['supervisor_plan'],'reviewer_required':True}}
        result = super().review_agents(state,config)
        self.phase5.store.update_supervisor(state['supervisor_run_id'],review_status='executed' if any(d['agent_id']=='reviewer' and self.phase5.store.run(d['agent_run_id'])['llm_calls'] for d in self.phase5.store.delegations(state['supervisor_run_id'])) else 'unavailable')
        return result

    def compress_review_input(self,state,value):
        # No raw PDF chunks or dataset outputs; preserve authoritative identifiers.
        before=count_tokens_approximately([HumanMessage(content=json.dumps(value,ensure_ascii=False))])
        value['evidence'] = [{k:e[k] for k in ('evidence_id','source_type','document_id','document_name','page','execution_id','dataset_ids','artifact_ids') if k in e} for e in state['evidence']]
        value['results'] = synthesis_context(state['shared_context'])['results']
        value['review_scope'] = 'Check collected source coverage, worker-result contradictions and evidence references. Cross-specialty comparison and final narrative assessment are performed by Supervisor AFTER this review; their absence from this provisional typed draft is not a retrieval gap.'
        value.pop('draft_synthesis',None)
        after=count_tokens_approximately([HumanMessage(content=json.dumps(value,ensure_ascii=False))])
        self.phase5.store.update_supervisor(state['supervisor_run_id'],review_context_reduction={'before':before,'after':after,
            'method':'Same Reviewer input before/after projection; count_tokens_approximately estimate'})
        return value

    def redelegate(self,state):
        review = state.get('review_result')
        stored = self.phase5.store.run(state['supervisor_run_id'])['metadata']
        if stored.get('revision_groups') or not review or review['verdict']=='pass' or state.get('review_round',0)>=min(1,self.config.get('MULTI_AGENT_REVIEW_MAX_ROUNDS',1)):
            return super().redelegate(state)
        records=[]; handled=set(); skipped=[]
        unresolved=set(review['missing_items']+review['unsupported_claims']+review['contradictions'])
        for suggestion in review['suggested_delegations']:
            item=suggestion['missing_item']
            if item not in unresolved or item in handled or len(records)>=min(2,self.config.get('MULTI_AGENT_REDELEGATION_MAX',2)):continue
            if gap_satisfied(item,state['shared_context'],state['evidence']):
                skipped.append(item);continue
            try:
                agent=self.phase5.registry.find_by_capability(suggestion['capability'],AgentRegistrySnapshot.model_validate(state['agent_snapshot']))[0]
            except (ValueError, KeyError):
                continue
            if agent.agent_id in {'supervisor','reviewer'}:continue
            previous=next((r for r in state['shared_context']['delegation_results'] if r['agent_id']==agent.agent_id),None)
            # A completed delegated objective must not be expanded with out-of-scope facts.
            if previous and previous['status']=='completed' and re.search(r'not reported|not provided|unavailable|unreported',item,re.I):continue
            docs=self.phase5.manager.workspaces.documents(state['workspace_id']) if state.get('workspace_id') else []
            target_text=item+' '+suggestion['objective']+' '+' '.join(previous['unresolved_questions'] if previous else [])
            named=[d for d in docs if d.display_name.casefold() in target_text.casefold()]
            label=re.search(r'paper\s+([a-z0-9])\b',target_text,re.I)
            if not named and label:
                named=[d for d in docs if re.search(r'(?:_|\b)'+re.escape(label[1])+r'\.pdf$',d.display_name,re.I)]
            sources=[d.id for d in named or docs] if agent.agent_id=='research' else state.get('data_asset_ids',[]) if agent.agent_id=='data' else []
            relevant=[e['evidence_id'] for e in state['evidence'] if (agent.agent_id=='research' and e['source_type'] in {'workspace','pdf','web','arxiv'}) or (agent.agent_id=='data' and e['source_type']=='analysis')]
            exact_items=[item]
            objective_details=suggestion['objective']
            if agent.agent_id=='research' and previous and named:
                prior_record=self.phase5.store.delegation(previous['delegation_id'])
                errors=self.phase5.store.run(prior_record['agent_run_id'])['metadata'].get('progress',{}).get('errors',[])
                source_errors=[q for q in errors if any(d.display_name.casefold() in q.casefold() for d in named)]
                if source_errors:
                    exact_items=source_errors
                    objective_details=' '.join(source_errors)
                elif re.search(r'experiment\.xlsx|chart|dataset|tabular',objective_details,re.I):
                    objective_details='Restore only missing reported source facts from '+', '.join(d.display_name for d in named)
            contract_repair=agent.agent_id=='data' and previous and any('Summary unavailable' in q for q in previous['unresolved_questions']) and relevant
            supplemental=SupplementaryDelegationRequest(exact_missing_items=exact_items,target_sources=sources,
                existing_evidence_ids=relevant,do_not_repeat=[r['summary'][:300] for r in state['shared_context']['delegation_results'] if r['status']=='completed'],
                expected_fields=['metrics','findings','artifact_ids'] if agent.agent_id=='data' else ['claim','evidence_id','page'])
            objective=('Repair only the structured result contract using existing computed evidence; do not inspect or analyze again.' if contract_repair else
                'Retrieve only the missing source facts: '+' '.join(exact_items)+'. '+objective_details+'. Treat facts not reported by target sources as explicit limitations, not failed retrieval. No cross-specialty analysis.')
            try:
                record=self.request(state,DelegationSpec(key='revision_'+str(len(records)),capability=suggestion['capability'],objective=objective[:3000]),
                    state['orchestration_group'],previous['delegation_id'] if previous else None,supplemental.model_dump())
                records.append(record);handled.add(item)
            except (BudgetExhausted,ValueError):break
        groups=[[r['id'] for r in records]] if records else []
        self.phase5.store.update_supervisor(state['supervisor_run_id'],revision_groups=groups,review_round=1,
            redelegation_count=len(records),satisfied_gaps_skipped=skipped)
        for record in records:self.phase5.event(state.get('task_id'),'REDELEGATION_CREATED',{'delegation_id':record['id'],'agent_id':record['agent_id'],'review_round':1})
        return {'orchestration_groups':groups,'orchestration_group':0,'review_round':1,'redelegation_count':len(records),'revision_pending':bool(records)}

    def orchestration_stop(self,state):
        budget=self.phase5.store.run(state['supervisor_run_id'])['metadata'].get('budget')
        if budget and budget['budget_exhausted']:
            return self.orchestration_complete(state)
        self.phase5.store.pause_budget(state['supervisor_run_id'])
        return super().orchestration_stop(state)
