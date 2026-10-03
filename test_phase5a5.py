"""Efficiency boundaries: real graphs/runtime with a clearly scripted provider."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from langchain_core.messages import HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from server.agent.efficiency import *
from server.agent.efficient_orchestration import efficient_decision, EfficientOrchestrationNodes
from server.agent.orchestration_schemas import DelegationSpec, DelegationResult, DelegationRequest
from server.agent_runs import AgentRunStore
from server.agent_registry import default_registry
from server.tool_policy import SecretFilter
from server.deterministic_analysis import deterministic_code
from server.analysis_runtime import validate_code
from test_phase5a import GraphFixture as _Fixture, FixtureModel as _Model, sample_request


class RouterTests(unittest.TestCase):
    def test_simple(self):self.assertEqual(efficient_decision('Hello',False).mode,'existing_workflow')
    def test_research(self):self.assertEqual(efficient_decision('Compare papers',False).mode,'single_agent')
    def test_data(self):self.assertEqual(efficient_decision('Analyze experiment.xlsx',True).required_capabilities,['data.analyze'])
    def test_statistics(self):self.assertEqual(efficient_decision('Find maximum miou',True).required_capabilities,['data.analyze'])
    def test_independent(self):
        d=efficient_decision('Compare papers with experiment.xlsx data',True)
        self.assertEqual((d.mode,d.dependency_type,d.independent_subtasks),('multi_agent','parallel',2))
    def test_sequential(self):
        d=efficient_decision('First extract metrics from papers then calculate with these data',True)
        self.assertEqual((d.mode,d.dependency_type),('existing_workflow','sequential'))
    def test_long_wording_only(self):self.assertNotEqual(efficient_decision('Please explain this carefully. '*100,False).mode,'multi_agent')
    def test_ambiguous(self):self.assertTrue(ambiguous('Investigate the project',efficient_decision('Investigate the project',False)))


class BudgetTests(unittest.TestCase):
    def test_partial_answer_survives_finalizer_budget_denial(self):
        from server.agent.orchestration import OrchestrationNodes
        def invoke(*args,**kwargs):raise BudgetExhausted('limit')
        nodes=SimpleNamespace(config={},supervisor_model=lambda _:SimpleNamespace(invoke=invoke))
        state={'supervisor_run_id':'r','final_answer':'Available calculation: A = 73.27 mIoU.', 'verification_result':{'grounded':False},'evidence':[]}
        result=OrchestrationNodes.answer_revision(nodes,state)
        self.assertIn('A = 73.27',result['final_answer']);self.assertIn('unverified',result['final_answer'])
    def test_soft_limit(self):
        b=BudgetManager.initial({'MULTI_AGENT_MAX_LLM_CALLS':10});b['llm_calls_used']=8
        self.assertTrue(BudgetManager.snapshot(b)['soft_reached'])
        self.assertFalse(b['budget_exhausted'])
    def test_finalizer_reserve(self):
        b=BudgetManager.initial({'MULTI_AGENT_MAX_LLM_CALLS':6})
        self.assertIsNone(BudgetManager.reserve(b,'llm_calls'))
        self.assertIsNone(BudgetManager.reserve(b,'llm_calls'))
        self.assertEqual(BudgetManager.reserve(b,'llm_calls'),'llm_calls')
        self.assertIsNone(BudgetManager.reserve(b,'llm_calls','grounding'))
    def test_token_known_limit(self):
        b=BudgetManager.initial({'MULTI_AGENT_MAX_TOTAL_TOKENS':100});b['known_token_subtotal']=101
        self.assertEqual(BudgetManager.reserve(b,'tool_calls'),'tokens')
    def test_unavailable_not_zero(self):self.assertIsNone(BudgetManager.initial({})['tokens_used'])
    def test_completed_elapsed_is_stable(self):
        b=BudgetManager.initial({});b['completed_at']=b['started_at']
        self.assertEqual(BudgetManager.snapshot(b)['elapsed_seconds'],0)
    def test_durable_pause_does_not_consume_execution_budget(self):
        b=BudgetManager.initial({'MULTI_AGENT_MAX_WALL_TIME_SECONDS':30})
        b['started_at']=(datetime.now(timezone.utc)-timedelta(seconds=601)).isoformat()
        b['paused_at']=(datetime.now(timezone.utc)-timedelta(seconds=600)).isoformat()
        self.assertLess(BudgetManager.snapshot(b)['elapsed_seconds'],2)
        BudgetManager.resume(b)
        self.assertIsNone(BudgetManager.reserve(b,'delegations'))
    def test_wall_time(self):
        b=BudgetManager.initial({'MULTI_AGENT_MAX_WALL_TIME_SECONDS':1});b['started_at']=(datetime.now(timezone.utc)-timedelta(seconds=3)).isoformat()
        self.assertEqual(BudgetManager.reserve(b,'delegations'),'wall_time')
    def test_delegation_hard(self):
        b=BudgetManager.initial({'MULTI_AGENT_MAX_DELEGATIONS':1});BudgetManager.reserve(b,'delegations')
        self.assertEqual(BudgetManager.reserve(b,'delegations'),'delegations')
    def test_no_new_work_after_exhaustion(self):
        b=BudgetManager.initial({'MULTI_AGENT_MAX_TOOL_CALLS':1});BudgetManager.reserve(b,'tool_calls');BudgetManager.reserve(b,'tool_calls')
        self.assertIsNotNone(BudgetManager.reserve(b,'delegations'))
    def test_atomic_concurrent_reservations(self):
        with tempfile.TemporaryDirectory() as folder:
            from server.tasks import TaskStore
            TaskStore(Path(folder)/'db.sqlite3')
            store=AgentRunStore(Path(folder)/'db.sqlite3',SecretFilter());store.efficiency_config={'MULTI_AGENT_EFFICIENCY_ENABLED':True,'MULTI_AGENT_MAX_LLM_CALLS':5}
            root=store.create_supervisor(None,'g','single_agent',{})
            def attempt(_):
                try:store.reserve_llm(root['id'],20,phase='grounding');return True
                except BudgetExhausted:return False
            with ThreadPoolExecutor(max_workers=5) as pool:accepted=list(pool.map(attempt,range(12)))
            self.assertEqual(sum(accepted),5)
            self.assertEqual(store.run(root['id'])['metadata']['budget']['llm_calls_used'],5)
            self.assertTrue(store.run(root['id'])['metadata']['budget']['budget_exhausted'])


class IdentityAndContextTests(unittest.TestCase):
    def test_finalizer_projection_preserves_computed_values_and_filename(self):
        output={'best':[{'model':'A','miou':73.27}],'ranked':[{'model':'B','miou':76},{'model':'A','miou':73.27}]}
        item={'evidence_id':'ev_actual','source_type':'analysis','content':json.dumps({'computed_output':json.dumps(output),'dataset_context':[{'dataset_id':'d','filename':'experiment.xlsx','inspection':{'huge':'x'*10000}}]})}
        projected=finalizer_evidence([item])[0]
        decoded=json.loads(projected['content'])
        self.assertEqual(decoded['computed_output'],output)
        self.assertEqual(decoded['dataset_context'],[{'dataset_id':'d','filename':'experiment.xlsx'}])
        self.assertLess(len(projected['content']),len(item['content']))
    def setUp(self):
        self.registry=default_registry({});self.request=sample_request(self.registry,task_id='t')
        self.scope={'source_versions':{'documents':[('doc','fingerprint1')]},'tool_snapshot':{'v':1},'cacheable_sources':True}
    def test_same_objective_identity(self):
        other=self.request.model_copy(update={'delegation_id':'other','objective':'  COLLECT   paper evidence '})
        self.assertEqual(cache_key(self.request,self.scope),cache_key(other,self.scope))
    def test_changed_dataset_miss(self):
        other=deepcopy(self.scope);other['source_versions']['datasets']=[('data','new')]
        self.assertNotEqual(cache_key(self.request,self.scope),cache_key(self.request,other))
    def test_changed_document_miss(self):
        other=deepcopy(self.scope);other['source_versions']['documents'][0]=('doc','fingerprint2')
        self.assertNotEqual(cache_key(self.request,self.scope),cache_key(self.request,other))
    def test_changed_context_miss(self):
        other=self.request.model_copy(deep=True);other.context.constraints=['new constraint']
        self.assertNotEqual(cache_key(self.request,self.scope),cache_key(other,self.scope))
    def test_contract_changed_miss(self):
        other=self.request.model_copy(update={'expected_output':'ContractV2'})
        self.assertNotEqual(cache_key(self.request,self.scope),cache_key(other,self.scope))
    def test_overlap_merge(self):
        specs=[DelegationSpec(key='a',capability='research.compare',objective='Compare methods in three papers'),DelegationSpec(key='b',capability='research.compare',objective='Analyze methods in same three papers')]
        kept,removed=overlap_specs(specs);self.assertEqual(len(kept),1);self.assertEqual(removed,[('b','a')])
    def test_different_permissions_not_merged(self):
        a=DelegationSpec(key='a',capability='research.compare',objective='Inspect repo');b=a.model_copy(update={'key':'b','capability':'code.review'})
        self.assertEqual(len(overlap_specs([a,b])[0]),2)
    def test_large_common_objective_does_not_merge_different_sources(self):
        shared='Inspect methodology parameters implementation reporting evidence confidence source descriptions constraints. '*10
        a=DelegationSpec(key='a',capability='research.compare',objective=shared+'paper_a.pdf')
        b=a.model_copy(update={'key':'b','objective':shared+'paper_b.pdf'})
        self.assertEqual(len(overlap_specs([a,b])[0]),2)
    def test_existing_valid_fact_skips_gap(self):
        context={'known_facts':[{'claim':'Paper C latency is 4 ms','evidence_ids':['ev_c']}]}
        self.assertTrue(gap_satisfied('Paper C latency',context,[{'evidence_id':'ev_c'}]))
        self.assertFalse(gap_satisfied('Paper C latency',context,[]))
    def test_measured_context_reduction(self):
        evidence=[{'evidence_id':'ev_'+str(i),'source_type':'workspace','document_id':'c' if i==8 else 'a','content':('latency data '+str(i)+' ')*1000,'page':1} for i in range(10)]
        scope={**self.scope,'selected_evidence':evidence,'selected_memory':[]}
        request=self.request.model_copy(update={'supplementary':{'target_sources':['c']}})
        after=AgentContextBuilder().build(request,scope)
        old=count_tokens_approximately([HumanMessage(content=json.dumps(scope))]);new=count_tokens_approximately([HumanMessage(content=json.dumps(after))])
        self.assertLess(new,old);self.assertEqual(after['selected_evidence'][0]['document_id'],'c')
        self.assertEqual(evidence[0]['content'],scope['selected_evidence'][0]['content'])


class DeterministicAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.inspection={'sheets':{'s':{'column_names':['model','params_m','miou'],'dtypes':{'model':'object','params_m':'float64','miou':'float64'}}}}
    def test_best_template_safe(self):
        code=deterministic_code('Find best configuration under 3M parameters and generate a chart',self.inspection)
        self.assertIsNotNone(code);validate_code(code)
    def test_no_unit_guessing(self):
        table=deepcopy(self.inspection);table['sheets']['s']['column_names'][1]='params'
        self.assertIsNone(deterministic_code('Find best under 3M',table))
    def test_advanced_falls_back(self):self.assertIsNone(deterministic_code('Analyze correlation under 3M',self.inspection))
    def test_mean(self):validate_code(deterministic_code('mean miou',self.inspection))
    def test_sort(self):validate_code(deterministic_code('sort miou descending',self.inspection))


class GraphTests(_Fixture):
    def test_hard_budget_shows_only_completed_calculation_as_partial(self):
        self.config['MULTI_AGENT_MAX_LLM_CALLS']=2
        task=self.task('Compare the three uploaded papers with experiment.xlsx. Find best under 3M parameters and generate a chart.')
        self.assertEqual(task.status,'COMPLETED')
        self.assertIn('Budget exhausted',task.metadata['answer'])
        self.assertIn('73.27',task.metadata['answer'])
        output=self.manager.phase3.tasks.steps(task.id)[-1].output_json
        self.assertFalse(output['verification_result']['grounded'])
    def test_contextual_followup_retains_query_rewriting(self):
        task=self.manager.phase3.task_service.create(self.workspace.id,'How does it compare with that method?', 'workspace_only')
        self.manager.store.save_messages(task.session_id,[HumanMessage(content='Earlier method discussion')])
        self.manager.phase3.task_service.run(task.id)
        self.assertIn('QueryAnalysis',[s for s,_ in _Model.captured])
    def test_extractive_revision_still_runs_second_semantic_verifier(self):
        from langchain_core.messages import AIMessage
        original_text=_Model.invoke;original_structured=_Model.with_structured_output
        caveat='No statistical significance was reported.'
        checks=[]
        def text(model,messages):
            value=original_text(model,messages);return AIMessage(content=value.content+'\n'+caveat)
        def structured(model,schema,**kwargs):
            result=original_structured(model,schema,**kwargs)
            if schema.__name__=='VerificationResult':
                def invoke(messages):
                    checks.append(messages)
                    return schema(grounded=len(checks)>1,confidence=.9,unsupported_claims=[] if len(checks)>1 else ['No significance claim unsupported'],unsupported_sentences=[] if len(checks)>1 else [caveat])
                return SimpleNamespace(invoke=invoke)
            return result
        with patch.object(_Model,'invoke',text),patch.object(_Model,'with_structured_output',structured):task=self.task('Compare the papers')
        self.assertEqual(task.status,'COMPLETED');self.assertEqual(len(checks),2)
        self.assertNotIn(caveat,task.metadata['answer'])
        self.assertEqual(self.manager.phase5.store.supervisor(task.id)['metadata']['deterministic_revision']['removed_sentences'],1)
    def test_overlapping_planner_work_runs_once(self):
        from server.agent.orchestration_schemas import SupervisorPlan
        _Model.plan_override=SupervisorPlan(goal='Compare methods',delegations=[
            DelegationSpec(key='a',capability='research.compare',objective='Compare methods in three papers'),
            DelegationSpec(key='b',capability='research.compare',objective='Analyze methods in same three papers')],execution_groups=[['a','b']]).model_dump()
        task=self.task('Compare the papers');self.assertEqual(task.status,'COMPLETED')
        self.assertEqual(self.research_count,1)
        self.assertEqual(self.manager.phase5.store.supervisor(task.id)['metadata']['duplicate_delegations_prevented'],1)
    def test_long_approval_wait_resumes_without_wall_budget_failure(self):
        _Model.write=True
        task=self.task('Compare papers with experiment data and create an issue in fixture/test')
        self.assertEqual(task.status,'WAITING_USER')
        store=self.manager.phase5.store;root=store.supervisor(task.id)
        budget=root['metadata']['budget'];self.assertIn('paused_at',budget)
        budget['started_at']=(datetime.now(timezone.utc)-timedelta(seconds=601)).isoformat()
        budget['paused_at']=(datetime.now(timezone.utc)-timedelta(seconds=600)).isoformat()
        store.update_supervisor(root['id'],budget=budget)
        approval=self.manager.phase4.actions.approvals(task.id)[0]
        self.manager.phase4.decide(approval['id'],'APPROVED')
        self.assertEqual(self.manager.phase3.tasks.get(task.id).status,'COMPLETED')
        self.assertEqual(len(json.loads(self.ledger.read_text())),1)
        self.assertNotIn('wall_time',store.supervisor(task.id)['metadata']['budget']['exhausted_reasons'])
    def test_research_contract_evaluates_its_own_goal(self):
        self.task('Compare the three uploaded papers with experiment.xlsx data. Find best under 3M parameters and generate a chart.')
        messages=next(m for s,m in _Model.captured if s=='ResearchAgentResult')
        brief=next(json.loads(m) for m in messages if m.startswith('{"objective"'))
        self.assertEqual(brief['context']['user_goal'],brief['objective'])
        self.assertNotIn('experiment.xlsx',brief['context']['user_goal'])
    def test_ambiguous_uses_one_router_call(self):
        from server.agent.orchestration_schemas import OrchestrationDecision
        original=_Model.with_structured_output
        def structured(model,schema,**kwargs):
            if schema is OrchestrationDecision:
                from unittest.mock import Mock
                def invoke(messages):
                    _Model.captured.append(('OrchestrationDecision',[m.content for m in messages]))
                    return schema(mode='single_agent',required_capabilities=['research.compare'],reason_summary='Research inspection suffices.')
                return Mock(invoke=invoke)
            return original(model,schema,**kwargs)
        with patch.object(_Model,'with_structured_output',structured):task=self.task('Investigate the project')
        self.assertEqual(task.status,'COMPLETED')
        schemas=[s for s,_ in _Model.captured]
        self.assertEqual(schemas.count('OrchestrationDecision'),1);self.assertNotIn('QueryAnalysis',schemas)
    def test_single_unresolved_triggers_review(self):
        self.config['MULTI_AGENT_SOFT_BUDGET_RATIO']=.01
        original=_Model.with_structured_output
        def structured(model,schema,**kwargs):
            result=original(model,schema,**kwargs)
            if schema.__name__=='ResearchAgentResult':
                def invoke(messages):return result.invoke(messages).model_copy(update={'unresolved_questions':['Source uncertainty remains.']})
                return SimpleNamespace(invoke=invoke)
            return result
        with patch.object(_Model,'with_structured_output',structured):self.task('Compare the papers')
        self.assertIn('ReviewerResult',[s for s,_ in _Model.captured])
    def test_single_contradiction_triggers_review(self):
        original=_Model.with_structured_output
        def structured(model,schema,**kwargs):
            result=original(model,schema,**kwargs)
            if schema.__name__=='ResearchAgentResult':
                def invoke(messages):
                    value=result.invoke(messages)
                    from server.agent.orchestration_schemas import Finding
                    return value.model_copy(update={'findings':[Finding(claim='method works',evidence_ids=value.evidence_ids),Finding(claim='not method works',evidence_ids=value.evidence_ids)]})
                return SimpleNamespace(invoke=invoke)
            return result
        with patch.object(_Model,'with_structured_output',structured):self.task('Compare the papers')
        self.assertIn('ReviewerResult',[s for s,_ in _Model.captured])
    def test_expired_wall_budget_completes_bounded_answer(self):
        self.config['MULTI_AGENT_MAX_WALL_TIME_SECONDS']=.000001
        task=self.task();self.assertEqual(task.status,'COMPLETED')
        cost=self.manager.phase5.metrics(self.manager.phase5.store.supervisor(task.id)['id'])['cost_trace']
        self.assertTrue(cost['budget_exhausted']);self.assertIn('wall_time',cost['budget']['exhausted_reasons'])
    def test_force_refresh_does_not_use_completed_cache(self):
        task=self.task('Compare the papers');store=self.manager.phase5.store;d=self.records(task)[0]
        request=DelegationRequest.model_validate(d['request'])
        scope={'source_versions':{'documents':[('paper','v1')]},'tool_snapshot':{},'cacheable_sources':True}
        store.annotate_run(d['agent_run_id'],cache_key=cache_key(request,scope))
        other=request.model_copy(update={'delegation_id':'refresh-request','force_refresh':True});store.create_delegation(other,8)
        nodes=object.__new__(EfficientOrchestrationNodes);nodes.phase5=self.manager.phase5;nodes.services=self.manager.phase3;nodes.config=self.config
        from server.agent.orchestration import OrchestrationNodes
        with patch.object(OrchestrationNodes,'agent_worker',return_value={'agent_outcomes':[]}) as actual:
            nodes.agent_worker({'delegation_request':other.model_dump(),'worker_scope':scope,'agent_snapshot':self.manager.phase5.registry.snapshot().model_dump()},{})
        actual.assert_called_once();self.assertFalse(store.run(store.delegation(other.delegation_id)['agent_run_id'])['metadata']['cache_hit'])
    def test_clear_mixed_shortcuts(self):
        task=self.task('Compare the three uploaded papers with experiment.xlsx results. Find best configuration under 3M parameters and generate a chart.')
        self.assertEqual(task.status,'COMPLETED')
        schemas=[s for s,_ in _Model.captured]
        self.assertNotIn('QueryAnalysis',schemas);self.assertNotIn('OrchestrationDecision',schemas);self.assertNotIn('SupervisorPlan',schemas)
        self.assertNotIn('AgentWorkPlan',schemas);self.assertNotIn('GeneratedCode',schemas);self.assertNotIn('DataAgentResult',schemas)
        self.assertIn('VerificationResult',schemas);self.assertIn('ReviewerResult',schemas)
        data=next(d for d in self.records(task) if d['agent_id']=='data')
        self.assertEqual(data['status'],'COMPLETED');self.assertEqual(data['result_json']['payload']['metrics']['best'][0]['model'],'A')
    def test_single_data_review_skipped(self):
        task=self.task('Find best configuration under 3M parameters in experiment.xlsx and generate a chart')
        root=self.manager.phase5.store.supervisor(task.id)
        self.assertEqual(root['metadata']['review_status'],'skipped')
        self.assertNotIn('ReviewerResult',[s for s,_ in _Model.captured])
    def test_single_research_review_skipped(self):
        task=self.task('Compare the papers')
        self.assertEqual(self.manager.phase5.store.supervisor(task.id)['metadata']['review_skip_reason'],'valid_single_result_no_open_questions')
    def test_always_review(self):
        self.config['MULTI_AGENT_REVIEW_POLICY']='always';self.manager.config['MULTI_AGENT_REVIEW_POLICY']='always'
        self.task('Compare the papers');self.assertIn('ReviewerResult',[s for s,_ in _Model.captured])
    def test_soft_budget_skips_optional_review(self):
        self.config['MULTI_AGENT_SOFT_BUDGET_RATIO']=.01
        task=self.task('Compare papers with experiment data')
        self.assertEqual(self.manager.phase5.store.supervisor(task.id)['metadata']['review_skip_reason'],'soft_budget_optional_review')
    def test_hard_model_budget_no_500(self):
        self.config['MULTI_AGENT_MAX_LLM_CALLS']=2
        task=self.task();self.assertEqual(task.status,'COMPLETED')
        self.assertTrue(self.manager.phase5.metrics(self.manager.phase5.store.supervisor(task.id)['id'])['budget_exhausted'])
    def test_hard_delegation_budget_preserves_first_branch(self):
        self.config['MULTI_AGENT_MAX_DELEGATIONS']=1
        task=self.task();self.assertEqual(task.status,'COMPLETED')
        self.assertEqual(len(self.records(task)),1)
        self.assertTrue(self.manager.phase5.metrics(self.manager.phase5.store.supervisor(task.id)['id'])['budget_exhausted'])
    def test_completed_cache_hit_without_worker_start(self):
        task=self.task('Compare the papers');store=self.manager.phase5.store
        original=self.records(task)[0];request=DelegationRequest.model_validate(original['request'])
        scope={'source_versions':{'documents':[('paper','v1')]},'tool_snapshot':{},'cacheable_sources':True}
        key=cache_key(request,scope);store.annotate_run(original['agent_run_id'],cache_key=key)
        other=request.model_copy(update={'delegation_id':'cached-request'})
        d=store.create_delegation(other,8)
        nodes=object.__new__(EfficientOrchestrationNodes);nodes.phase5=self.manager.phase5;nodes.services=self.manager.phase3;nodes.config=self.config
        state={'delegation_request':other.model_dump(),'worker_scope':scope,'agent_snapshot':self.manager.phase5.registry.snapshot().model_dump()}
        result=nodes.agent_worker(state,{})
        self.assertEqual(result['agent_outcomes'][0]['status'],'completed')
        self.assertEqual(store.run(d['agent_run_id'])['llm_calls'],0)
        self.assertTrue(store.run(d['agent_run_id'])['metadata']['cache_hit'])
        self.assertEqual(self.manager.phase5.metrics(request.supervisor_run_id)['cost_trace']['retrieval_calls'],1)
    def test_partial_cache_miss(self):
        task=self.task('Compare the papers');store=self.manager.phase5.store;d=self.records(task)[0]
        request=DelegationRequest.model_validate(d['request']);request=request.model_copy(update={'delegation_id':'partial-request'})
        fresh=store.create_delegation(request,8);store.annotate_run(fresh['agent_run_id'],cache_key='partial-key')
        store.finish(request.delegation_id,DelegationResult(delegation_id=request.delegation_id,agent_id='research',status='partial',summary='gap'))
        self.assertIsNone(store.cached(task.id,request.supervisor_run_id,'partial-key'))
    def test_targeted_redelegation_request(self):
        _Model.revision=True
        task=self.task();revisions=[d for d in self.records(task) if d['request'].get('revision_of')]
        self.assertEqual(len(revisions),1)
        supplemental=revisions[0]['request']['supplementary']
        self.assertEqual(supplemental['exact_missing_items'],['Paper C latency'])
        self.assertIn('page',supplemental['expected_fields'])
        self.assertIn('only the missing',revisions[0]['objective'])
    def test_cost_trace_keeps_unavailable_usage(self):
        task=self.task();cost=self.manager.phase5.metrics(self.manager.phase5.store.supervisor(task.id)['id'])['cost_trace']
        self.assertIsNone(cost['tokens_prompt']);self.assertIsNone(cost['tokens_completion'])
        self.assertEqual(cost['total_llm_calls'],sum(cost[k] for k in ['supervisor_llm_calls','research_llm_calls','data_llm_calls','coding_llm_calls','reviewer_llm_calls','finalizer_llm_calls']))


class TargetedRetrieverTests(unittest.TestCase):
    def test_missing_c_queries_only_c_index(self):
        from server.rag.workspace_retrieval import WorkspaceRetriever
        from langchain_core.documents import Document
        records=[SimpleNamespace(id=name,display_name=name+'.pdf') for name in ['paper_a','paper_b','paper_c']]
        retriever=WorkspaceRetriever(SimpleNamespace(documents=lambda _:records),'w',{'RERANKER_ENABLED':False},lambda _:None)
        seen=[]
        def retrieve(record,query):
            seen.append((record.id,query));return [Document(page_content='latency 4 ms',metadata={'document_id':record.id,'chunk_id':record.id})]
        try:
            with patch.object(retriever,'_retrieve',retrieve):hits=retriever.invoke('Paper C latency',document_ids=['paper_c'])
            self.assertEqual(seen,[('paper_c','Paper C latency')]);self.assertEqual(len(hits),1)
        finally:retriever._executor.shutdown(wait=True)


class EvaluationCriteriaTests(unittest.TestCase):
    def test_explicit_best_a_with_markdown_parenthesis(self):
        from evaluation.multi_agent_efficiency.metrics import quality
        q=quality('The best eligible configuration is **A (`2.5M`, mIoU `73.27`)**. B 3.2M is excluded. No causal claim.',[],[],[],True)
        self.assertTrue(q['core_answer_correct'])
        q=quality('The best was **A (2.5M, mIoU 73.27)**. B 3.2M is excluded. No causal claim.',[],[],[],True)
        self.assertTrue(q['core_answer_correct'])
    def test_numeric_revision_never_uses_sentence_pruning(self):
        from server.agent.orchestration import OrchestrationNodes
        nodes=object.__new__(EfficientOrchestrationNodes)
        state={'final_answer':'No model exceeds 3M.','verification_result':{'unsupported_sentences':['No model exceeds 3M.']}}
        with patch.object(OrchestrationNodes,'answer_revision',return_value={'fallback':True}) as fallback:
            self.assertEqual(nodes.answer_revision(state),{'fallback':True})
        fallback.assert_called_once()
    def test_unknown_span_never_uses_sentence_pruning(self):
        from server.agent.orchestration import OrchestrationNodes
        nodes=object.__new__(EfficientOrchestrationNodes)
        with patch.object(OrchestrationNodes,'answer_revision',return_value={}) as fallback:
            nodes.answer_revision({'final_answer':'Available facts','verification_result':{'unsupported_sentences':['No invented span.']}})
        fallback.assert_called_once()
    def test_method_a_is_a_valid_explicit_best_choice(self):
        from evaluation.multi_agent_efficiency.metrics import quality
        q=quality('Best: Method A — 2.5M, 73.27. B at 3.2M is excluded. No causal claim.',[],[],[],True)
        self.assertTrue(q['core_answer_correct'])
    def test_best_b_does_not_pass_from_a_in_comparison(self):
        from evaluation.multi_agent_efficiency.metrics import quality
        q=quality('Method A 2.5M, 73.27.\nBest: Method B 3.2M. Exclude C. No causal claim.',[],[],[],True)
        self.assertFalse(q['core_answer_correct'])
        q=quality('Model A 2.5M, 73.27 is not the best.\nBest: Method B 3.2M. Exclude C. No causal claim.',[],[],[],True)
        self.assertFalse(q['core_answer_correct'])
    def test_naked_citation_is_not_coverage(self):
        from evaluation.multi_agent_efficiency.metrics import quality
        q=quality('Best: Model A 2.5M 73.27. Exclude B 3.2M. No causal claim.',[],[{'evidence_id':'fake'}],[],True)
        self.assertFalse(q['citation_validity']);self.assertFalse(q['required_evidence_coverage']['three_papers'])


if __name__=='__main__':unittest.main()
