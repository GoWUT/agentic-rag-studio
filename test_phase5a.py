"""Bounded orchestration acceptance: actual graphs, SQLite, SDK and analysis runtime."""
import asyncio
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import ValidationError
from server.config import load_config
from server.sessions import AgentSessionManager
from server.agent.orchestration import routing_candidate, validate_plan, deterministic_review, synthesis_context
from server.agent.orchestration_schemas import *
from server.agent_registry import default_registry, AgentUnavailable, CapabilityUnavailable, DelegationRejected
from server.agent_runs import merge_context
from server.agent.evidence import Evidence, merge_evidence_items
from server.mcp_client import MCPToolProvider, MCPServerConfig
from server.phase3_api import register_phase3_api
from server.phase4_api import register_phase4_api
from server.phase5_api import register_phase5_api
from server.tasks import TaskConflict
from evaluation.phase4_mock_server import build_server
from test_phase2 import document


class FixtureModel:
    """Deterministic provider fixture; not reported as a real LLM."""
    captured = []
    revision = False
    write = False
    bad_tool = None
    fail_reviewer = False
    fail_summary = False
    plan_override = None
    duplicate_caps = False
    inline_analysis_code = False
    lock = threading.Lock()

    def __init__(self, **kwargs):
        pass

    @staticmethod
    def objects(messages):
        values = []
        for m in messages:
            if isinstance(m, HumanMessage):
                try:
                    values.append(json.loads(m.content))
                except (ValueError, TypeError):
                    pass
        return values

    def with_structured_output(self, schema, **kwargs):
        def invoke(messages):
            with self.lock:
                self.captured.append((schema.__name__, [m.content for m in messages]))
            objects = self.objects(messages)
            last = objects[-1] if objects else {}
            if schema.__name__ == 'QueryAnalysis':
                goal = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
                return schema(standalone_query=goal, query_type='research', needs_retrieval=True, selected_sources=['workspace'])
            if schema.__name__ == 'OrchestrationDecision':
                if self.duplicate_caps:
                    value=deepcopy(last['candidate'])
                    value['required_capabilities']=['research.document','research.compare']
                    return schema.model_validate(value)
                return schema.model_validate(last['candidate'])
            if schema.__name__ == 'SupervisorPlan':
                if self.plan_override:
                    return schema.model_validate(self.plan_override)
                caps = last['required_capabilities']
                specs = [DelegationSpec(key='d'+str(i), capability=c, objective={'research.compare': 'Collect paper evidence only.',
                    'data.analyze': 'Analyze experiment data only.', 'code.review': 'Inspect code only.'}.get(c, c)) for i, c in enumerate(caps)]
                actions = [AgentToolCall(capability='github.issue.create', arguments={'owner':'fixture','repo':'test','title':'Phase5 fixture','body':'Synthetic only'})] if self.write else []
                return schema(goal=last['goal'], delegations=specs, execution_groups=[[s.key for s in specs]], reviewer_required=len(caps)>1, actions=actions)
            if schema.__name__ == 'AgentWorkPlan':
                tools = last['tools']
                caps = {t['capability'] for t in tools}
                if self.bad_tool:
                    return schema(steps=[AgentToolCall(capability=self.bad_tool, arguments={})])
                if 'data.analyze' in caps:
                    ids = objects[0]['context']['dataset_ids']
                    arguments = {'dataset_ids':ids, 'objective':'Find the best configuration under 3M parameters and generate a chart.'}
                    if self.inline_analysis_code:
                        arguments['code'] = "import os\nprint(os.environ)"
                    return schema(steps=[AgentToolCall(capability='dataset.inspect', arguments={'dataset_ids':ids}),
                        AgentToolCall(capability='data.analyze', arguments=arguments)])
                if 'workspace.search' in caps:
                    return schema(steps=[AgentToolCall(capability='workspace.search', arguments={'query':objects[0]['objective']})])
                if 'github.file.read' in caps:
                    return schema(steps=[AgentToolCall(capability='github.file.read', arguments={'owner':'fixture','repo':'test','path':'README.md'})])
                return schema()
            if schema.__name__ in {'ResearchAgentResult','DataAgentResult','CodingAgentResult'}:
                if self.fail_summary:
                    raise RuntimeError('Fixture summary failure')
                ids = [e['evidence_id'] for e in last['evidence']]
                return schema(delegation_id=last['delegation_id'], status='completed' if ids else 'failed', summary='Structured collected findings.',
                    findings=[Finding(claim='Synthetic supported finding', evidence_ids=ids, confidence=.9)] if ids else [], evidence_ids=ids, confidence=.9)
            if schema.__name__ == 'GeneratedCode':
                return schema(code="import matplotlib.pyplot as plt\neligible = df[df['params_m'] < 3]\nbest = eligible.sort_values('miou', ascending=False).iloc[0]\nprint(best.to_dict())\nplt.bar(df['model'], df['miou'])\nplt.savefig('comparison.png')\nplt.close()")
            if schema.__name__ == 'ReviewerResult':
                if self.fail_reviewer:
                    raise RuntimeError('Reviewer fixture unavailable')
                if self.revision:
                    return schema(verdict='needs_revision', missing_items=['Paper C latency'], suggested_delegations=[SuggestedDelegation(capability='research.document',
                        objective='Retrieve Paper C latency evidence only.', missing_item='Paper C latency')], confidence=.8)
                return schema(verdict='pass', confidence=.9)
            if schema.__name__ == 'VerificationResult':
                return schema(grounded=True, confidence=.9)
            if schema.__name__ == 'MemoryCandidates':
                return schema(candidates=[])
            if schema.__name__ == 'TaskPlan':
                goal = last.get('goal', 'Existing')
                import re
                steps = []
                if re.search(r'paper|document',goal,re.I):
                    steps.append({'id':'research','description':'Retrieve papers','query':goal,'sources':['workspace'],'preferred_tool':'search_workspace'})
                if re.search(r'data|experiment|chart|csv|xlsx',goal,re.I) and last.get('datasets'):
                    steps.extend([{'id':'inspect','description':'Inspect data','query':goal,'sources':[],'preferred_tool':'inspect_dataset'},
                        {'id':'analyze','description':'Analyze data','query':goal,'sources':[],'preferred_tool':'analyze_data'}])
                return schema(goal=goal,steps=steps or [{'id':'one','description':'Retrieve','query':goal,'sources':['workspace'],'preferred_tool':'search_workspace'}])
            raise ValueError('Unsupported fixture schema')
        return Mock(invoke=invoke)

    def invoke(self, messages):
        with self.lock:
            self.captured.append(('Text',[m.content for m in messages]))
        values = self.objects(messages)
        value = values[-1] if values else {}
        for message in messages:
            if str(message.content).startswith('Adaptive workflow context:\n'):
                value = json.loads(message.content.split('\n',1)[1])
        ids = [e['evidence_id'] for e in value.get('evidence_store', value.get('evidence', []))]
        return AIMessage(content='Synthetic supported comparison. ' + ' '.join('['+i+']' for i in ids))


def sample_request(registry, **updates):
    agent = registry.get('research')
    value = DelegationRequest(supervisor_run_id='supervisor-fixture', agent_id='research', objective='Collect paper evidence',
        required_capabilities=['research.document'], context=DelegationContext(user_goal='Goal'),
        expected_output=agent.output_contract, allowed_tool_capabilities=agent.tool_capabilities,
        max_iterations=agent.max_iterations, max_tool_calls=agent.max_tool_calls)
    return value.model_copy(update=updates)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = default_registry({})
        self.snapshot = self.registry.snapshot()

    def test_all_identities(self):
        self.assertEqual({a.agent_id for a in self.registry.list_agents()}, {'supervisor','research','data','coding','reviewer'})

    def test_capability_selection(self):
        self.assertEqual(self.registry.find_by_capability('data.visualize')[0].agent_id, 'data')

    def test_unknown_agent(self):
        with self.assertRaises(AgentUnavailable): self.registry.get('invented')

    def test_unknown_capability(self):
        with self.assertRaises(CapabilityUnavailable): self.registry.find_by_capability('agent.create')

    def test_disable_enable(self):
        self.registry.disable('data')
        with self.assertRaises(AgentUnavailable): self.registry.get('data')
        self.registry.enable('data')
        self.assertTrue(self.registry.get('data').enabled)

    def test_snapshot_stability(self):
        self.registry.disable('research')
        self.assertTrue(self.registry.get('research', self.snapshot).enabled)

    def test_snapshot_copy_isolation(self):
        self.snapshot.agents['research'].tool_capabilities.append('shell')
        self.assertNotIn('shell', self.registry.get('research').tool_capabilities)

    def test_no_duplicate_registration(self):
        with self.assertRaises(ValueError): self.registry.register(self.registry.get('data'))

    def test_no_worker_delegation(self):
        registry = default_registry({})
        with self.assertRaises(DelegationRejected): registry.register(self.registry.get('research').model_copy(update={'agent_id':'recursive','can_delegate':True}))

    def validate(self, request, **kwargs):
        return self.registry.validate(request, self.snapshot, config={}, **kwargs)

    def test_valid_delegation(self):
        self.assertEqual(self.validate(sample_request(self.registry)).agent_id, 'research')

    def test_unknown_delegation_agent(self):
        with self.assertRaises(AgentUnavailable): self.validate(sample_request(self.registry, agent_id='unknown'))

    def test_capability_mismatch(self):
        with self.assertRaises(CapabilityUnavailable): self.validate(sample_request(self.registry, required_capabilities=['data.analyze']))

    def test_tool_permission_violation(self):
        with self.assertRaises(DelegationRejected): self.validate(sample_request(self.registry, allowed_tool_capabilities=['github.issue.create']))

    def test_budget_violation(self):
        with self.assertRaises(DelegationRejected): self.validate(sample_request(self.registry, max_tool_calls=99))

    def test_delegation_limit(self):
        with self.assertRaises(DelegationRejected): self.validate(sample_request(self.registry), delegation_count=8)

    def test_depth_limit(self):
        with self.assertRaises(DelegationRejected): self.validate(sample_request(self.registry, depth=2))

    def test_cancelled_delegation(self):
        with self.assertRaises(DelegationRejected): self.validate(sample_request(self.registry), cancelled=True)

    def test_wrong_result_contract(self):
        with self.assertRaises(DelegationRejected): self.validate(sample_request(self.registry, expected_output='prose'))

    def test_result_schema_rejects_chat_history(self):
        with self.assertRaises(ValidationError): DelegationResult(delegation_id='d',agent_id='research',status='completed',summary='s',messages=[])

    def test_context_schema_rejects_raw_chats(self):
        with self.assertRaises(ValidationError): DelegationContext(user_goal='g',all_agent_messages=[])


class RouterTests(unittest.TestCase):
    def test_simple_qa(self): self.assertEqual(routing_candidate('Hello',False).mode,'existing_workflow')
    def test_single_pdf(self): self.assertEqual(routing_candidate('Summarize this PDF',False).mode,'existing_workflow')
    def test_data_only(self): self.assertEqual(routing_candidate('Analyze experiment.csv',True).required_capabilities,['data.analyze'])
    def test_research_only(self): self.assertEqual(routing_candidate('Compare the papers',False).required_capabilities,['research.compare'])
    def test_paper_experimental_results_do_not_activate_data(self):
        self.assertEqual(routing_candidate('Compare the methods and experimental results of the papers in this workspace',True).required_capabilities,['research.compare'])
    def test_research_data(self): self.assertEqual(routing_candidate('Compare papers and experiment data',True).mode,'multi_agent')
    def test_code_only(self): self.assertEqual(routing_candidate('Review workflow implementation',False).required_capabilities,['code.review'])
    def test_research_code(self): self.assertEqual(routing_candidate('Compare documentation with actual implementation',False).mode,'multi_agent')
    def test_missing_data_not_invented(self): self.assertNotIn('data.analyze',routing_candidate('Compare papers and experiment.csv',False).required_capabilities)
    def test_paper_encoder_or_model_architecture_is_not_code_inspection(self):
        for goal in ('Compare the encoder and decoder methods in the papers', '比较论文中的网络架构与方法设计'):
            self.assertEqual(routing_candidate(goal,False).required_capabilities,['research.compare'])


class ContextReviewTests(unittest.TestCase):
    def setUp(self):
        self.evidence = [Evidence(evidence_id='ev_one',source_type='web',title='Fixture',content='Supported fact').model_dump()]
        self.result = DelegationResult(delegation_id='one',agent_id='research',status='completed',summary='Summary',evidence_ids=['ev_one'],
            payload={'findings':[Finding(claim='Supported fact',evidence_ids=['ev_one']).model_dump()]})
        self.context = merge_context(SharedTaskContext(goal='Goal'),[self.result],self.evidence,[])

    def test_context_merge(self): self.assertEqual(self.context.completed_delegations,['one'])
    def test_dedup_result_merge(self): self.assertEqual(len(merge_context(self.context,[self.result],self.evidence,[]).delegation_results),1)
    def test_unknown_evidence_rejected(self):
        with self.assertRaises(ValueError): merge_context(self.context,[self.result.model_copy(update={'delegation_id':'two','evidence_ids':['ev_fake']})],self.evidence,[])
    def test_conflicting_results_rejected(self):
        with self.assertRaises(ValueError): merge_context(self.context,[self.result.model_copy(update={'summary':'Conflicting'})],self.evidence,[])
    def test_complete_review_passes(self): self.assertEqual(deterministic_review(self.context,self.evidence,[]).verdict,'pass')
    def test_missing_evidence_review_gap(self):
        context = SharedTaskContext(goal='g',delegation_results=[DelegationResult(delegation_id='x',agent_id='research',status='partial',summary='Missing')])
        self.assertTrue(deterministic_review(context,[],[]).suggested_delegations)
    def test_unsupported_claim_detected(self):
        self.context.delegation_results[0].payload['findings'][0]['evidence_ids'] = []
        self.assertTrue(deterministic_review(self.context,self.evidence,[]).unsupported_claims)
    def test_contradiction_detected(self):
        self.context.delegation_results[0].payload['findings'].append(Finding(claim='not Supported fact',evidence_ids=['ev_one']).model_dump())
        self.assertTrue(deterministic_review(self.context,self.evidence,[]).contradictions)
    def test_invalid_parallel_result_conflict(self):
        with self.assertRaises(ValueError): combine_results([self.result.model_dump()],[self.result.model_copy(update={'summary':'other'}).model_dump()])
    def test_retrieval_score_changes_are_not_content_conflicts(self):
        result=merge_evidence_items(self.evidence,[{**self.evidence[0],'retrieval_score':.5,'rerank_score':.9}])
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['rerank_score'],.9)
    def test_source_content_conflict_rejected(self):
        with self.assertRaises(ValueError): merge_evidence_items(self.evidence,[{**self.evidence[0],'content':'Different source'}])

    def test_large_metric_payload_has_valid_bounded_synthesis_projection(self):
        context = self.context.model_dump()
        context['delegation_results'][0]['payload']['metrics'] = {'raw_rows': ['x' * 10000], 'best_model': 'A'}
        projected = synthesis_context(context)
        serialized = json.dumps(projected)
        self.assertLess(len(serialized), 3000)
        self.assertEqual(json.loads(serialized)['results'][0]['metrics']['best_model'], 'A')
        self.assertEqual(projected['results'][0]['evidence_ids'], ['ev_one'])


class GraphFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        FixtureModel.captured = []
        FixtureModel.revision = FixtureModel.write = FixtureModel.fail_reviewer = FixtureModel.fail_summary = False
        FixtureModel.bad_tool = None
        FixtureModel.plan_override = None
        FixtureModel.duplicate_caps = False
        FixtureModel.inline_analysis_code = False
        self.config = {**load_config(), 'WORKSPACE_DIR':str(self.root),'MULTI_AGENT_ENABLED':True,'GITHUB_MCP_ENABLED':False,
            'MCP_SERVERS_FILE':'','MEMORY_ENABLED':False,'MEMORY_AUTO_EXTRACT':False,'GROUNDING_CHECK_ENABLED':True,
            'GITHUB_MCP_READ_ONLY':False,'GITHUB_ALLOWED_REPOSITORIES':'fixture/test','AGENT_MAX_GRAPH_STEPS':55}
        self.manager = AgentSessionManager(self.config)
        self.workspace = self.manager.workspaces.create('Phase5 fixture')
        self.manager.workspaces.register(document(self.workspace.id,'paper'))
        self.asset = self.manager.phase3.data.upload(self.workspace.id,'experiment.csv',BytesIO(b'model,params_m,miou\nA,2.5,73.27\nB,3.2,76\nC,2,70\n'),'text/csv')
        self.ledger = self.root/'ledger.json'
        self.provider = MCPToolProvider(MCPServerConfig(id='github',url='https://api.githubcopilot.com/mcp/'),self.config,in_memory_server=build_server(self.ledger))
        self.manager.phase4.registry.register_provider(self.provider)
        asyncio.run(self.manager.phase4.registry.refresh())
        self.research_count = 0
        def search_workspace(query: str):
            """Search synthetic paper fixtures."""
            self.research_count += 1
            return {'evidence':[Evidence(evidence_id='ev_paper',source_type='workspace',document_id='paper',document_name='paper.pdf',page=1,page_count=2,
                content='Synthetic method A achieves mIoU 73.27 with 2.5 million parameters.').model_dump()]}
        self.tools = [StructuredTool.from_function(search_workspace)]
        self.patch_model = patch('server.agent.graph.ChatOpenAI',FixtureModel)
        self.patch_model.start()
        self.addCleanup(self.patch_model.stop)
        self.patch_tools = patch('server.sessions.build_tools',lambda *a,**k:self.tools)
        self.patch_tools.start()
        self.addCleanup(self.patch_tools.stop)

    def task(self, goal='Compare papers with experiment data and check conclusions', scope='workspace_and_external', **kwargs):
        task = self.manager.phase3.task_service.create(self.workspace.id,goal,scope)
        return self.manager.phase3.task_service.run(task.id,**kwargs)

    def records(self, task): return self.manager.phase5.store.delegations(task_id=task.id)


class GraphTests(GraphFixture):
    def test_single_role_capability_aliases_do_not_create_multiple_agents(self):
        FixtureModel.duplicate_caps = True
        task=self.task('Compare papers')
        self.assertEqual([d['agent_id'] for d in self.records(task)],['research'])

    def test_feature_disable_does_not_break_existing_task_resume(self):
        task=self.task(max_steps=1)
        self.config['MULTI_AGENT_ENABLED']=False
        self.manager=AgentSessionManager(self.config)
        resumed=self.manager.phase3.task_service.run(task.id,resume=True)
        self.assertEqual(resumed.status,'COMPLETED')
        self.assertEqual(self.research_count,1)

    def test_enabling_feature_preserves_a_paused_legacy_task_plan(self):
        self.config['MULTI_AGENT_ENABLED'] = False
        task = self.task(max_steps=1)
        self.assertEqual(task.status, 'PAUSED')
        self.assertFalse(self.records(task))
        self.config['MULTI_AGENT_ENABLED'] = True
        self.manager = AgentSessionManager(self.config)
        resumed = self.manager.phase3.task_service.run(task.id, resume=True)
        self.assertEqual(resumed.status, 'COMPLETED')
        self.assertFalse(self.records(task))
        self.assertEqual(self.research_count, 1)

    def test_cancel_marks_supervisor_and_pending_agents_preserving_completed_work(self):
        task = self.task(max_steps=1)
        store = self.manager.phase5.store
        supervisor = store.supervisor(task.id)
        completed_ids = [r['id'] for r in store.runs(task.id) if r['status'] == 'COMPLETED']
        pending = store.create_delegation(sample_request(self.manager.phase5.registry,
            task_id=task.id, supervisor_run_id=supervisor['id']), 8)
        self.manager.phase3.tasks.transition(task.id, 'CANCELLED')
        store.cancel(task.id)
        self.assertEqual(store.run(supervisor['id'])['status'], 'CANCELLED')
        self.assertEqual(store.run(pending['agent_run_id'])['status'], 'CANCELLED')
        self.assertTrue(all(store.run(i)['status'] == 'COMPLETED' for i in completed_ids))

    def test_research_data_tools_overlap(self):
        barrier = threading.Barrier(2, timeout=10)
        original_search = self.tools[0].func
        original_inspect = self.manager.phase3.data.inspect
        seen = set()
        guard = threading.Lock()
        def overlap(kind):
            with guard:
                first = kind not in seen
                seen.add(kind)
            if first:
                barrier.wait()
        def search(query: str):
            overlap('research')
            return original_search(query)
        def inspect(*args):
            overlap('data')
            return original_inspect(*args)
        self.tools[0].func = search
        with patch.object(self.manager.phase3.data, 'inspect', side_effect=inspect):
            task = self.task()
        self.assertEqual(task.status, 'COMPLETED')
        self.assertFalse(barrier.broken)
        self.assertEqual(seen, {'research','data'})

    def test_max_parallel_respected(self):
        from server.agent.orchestration import OrchestrationNodes
        self.config['MULTI_AGENT_MAX_PARALLEL'] = 2
        FixtureModel.plan_override = SupervisorPlan(goal='g', delegations=[
            DelegationSpec(key='r1',capability='research.document',objective='One'),
            DelegationSpec(key='r2',capability='code.inspect',objective='Code'),
            DelegationSpec(key='d',capability='data.analyze',objective='Data')], execution_groups=[['r1','r2','d']]).model_dump()
        active, peak = 0, 0
        guard = threading.Lock()
        original = OrchestrationNodes.agent_worker
        def counted(nodes, state, config):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.1)
                return original(nodes,state,config)
            finally:
                with guard:
                    active -= 1
        with patch.object(OrchestrationNodes,'agent_worker',counted):
            task = self.task('Compare papers and experiment data with the code implementation')
        self.assertEqual(task.status,'COMPLETED')
        self.assertEqual(peak,2)

    def test_sequential_dependencies(self):
        FixtureModel.plan_override = SupervisorPlan(goal='g', delegations=[
            DelegationSpec(key='r',capability='research.document',objective='Collect papers'),
            DelegationSpec(key='d',capability='data.analyze',objective='Analyze',depends_on=['r'])], execution_groups=[['r'],['d']]).model_dump()
        task = self.task('First collect papers, then analyze experiment data based on the methods')
        records = self.records(task)
        research = next(d for d in records if d['agent_id']=='research')
        data = next(d for d in records if d['agent_id']=='data')
        self.assertLessEqual(research['completed_at'],data['started_at'])
        data_contexts = [json.loads(messages[-2]) for schema,messages in FixtureModel.captured if schema=='AgentWorkPlan' and 'data.analyze' in messages[-1]]
        self.assertTrue(data_contexts[0]['context']['evidence_ids'])

    def test_research_and_data_complete_with_grounding(self):
        task = self.task()
        self.assertEqual(task.status,'COMPLETED')
        self.assertEqual({d['agent_id'] for d in self.records(task)},{'research','data','reviewer'})
        self.assertTrue(task.metadata['answer'])
        self.assertEqual(self.research_count,1)
        self.assertTrue(self.manager.phase3.data.artifacts(task.id))
        final_step = self.manager.phase3.tasks.steps(task.id)[-1]
        self.assertTrue(final_step.output_json['verification_result']['grounded'])
        self.assertEqual(final_step.output_json['revision_count'], 0)
        research_contracts = [json.loads(messages[-1]) for schema, messages in FixtureModel.captured if schema == 'ResearchAgentResult']
        source = next(e for e in research_contracts[0]['evidence'] if e['evidence_id'] == 'ev_paper')
        self.assertEqual((source['document_name'], source['page']), ('paper.pdf', 1))

    def test_data_only_uses_one_specialist(self):
        task = self.task('Analyze experiment.csv and chart the best configuration')
        self.assertEqual([d['agent_id'] for d in self.records(task)],['data'])

    def test_compound_analysis_is_not_reduced_to_row_count(self):
        task = self.task('Report row count and select the best experiment.csv configuration, then generate a chart')
        self.assertTrue(self.manager.phase3.data.artifacts(task.id))
        self.assertTrue(any(schema == 'GeneratedCode' for schema, _ in FixtureModel.captured))

    def test_data_planner_code_uses_constrained_generation_boundary(self):
        FixtureModel.inline_analysis_code = True
        task = self.task('Analyze experiment.csv and generate a chart')
        self.assertTrue(self.manager.phase3.data.artifacts(task.id))
        data = next(d for d in self.records(task) if d['agent_id'] == 'data')
        plan = self.manager.phase5.store.run(data['agent_run_id'])['metadata']['progress']['work_plan']
        self.assertNotIn('code', plan['steps'][1]['arguments'])

    def test_research_only_uses_one_specialist(self):
        task = self.task('Compare the methods in these papers')
        self.assertEqual([d['agent_id'] for d in self.records(task)],['research'])

    def test_coding_uses_read_only_mcp(self):
        task = self.task('Review workflow implementation in repository fixture/test',scope='external')
        self.assertEqual(task.status,'COMPLETED')
        self.assertEqual([d['agent_id'] for d in self.records(task)],['coding'])
        self.assertFalse(self.ledger.exists())

    def test_mixed_research_code(self):
        task = self.task('Compare documentation with actual implementation')
        self.assertEqual({d['agent_id'] for d in self.records(task)},{'research','coding','reviewer'})

    def test_bounded_gap_redelegates_only_research(self):
        FixtureModel.revision = True
        task = self.task()
        revisions = [d for d in self.records(task) if d['request']['revision_of']]
        self.assertEqual([d['agent_id'] for d in revisions],['research'])
        self.assertIn('Paper C latency',revisions[0]['objective'])
        self.assertEqual(sum(d['agent_id']=='data' for d in self.records(task)),1)
        self.assertEqual(sum(d['agent_id']=='reviewer' for d in self.records(task)),1)

    def test_review_disabled(self):
        self.config['MULTI_AGENT_REVIEW_ENABLED'] = False
        task = self.task()
        self.assertNotIn('reviewer',[d['agent_id'] for d in self.records(task)])

    def test_completed_reviewer_is_cached_before_supervisor_checkpoint(self):
        from server.agent.orchestration import OrchestrationNodes
        original = OrchestrationNodes.review_agents
        captured = {}
        def observe(nodes, state, config):
            captured.update(nodes=nodes, state=deepcopy(state))
            return original(nodes, state, config)
        with patch.object(OrchestrationNodes, 'review_agents', observe):
            task = self.task()
        supervisor = self.manager.phase5.store.supervisor(task.id)
        self.manager.phase5.store.update_supervisor(supervisor['id'], review_result=None)
        calls_before = sum(schema == 'ReviewerResult' for schema, _ in FixtureModel.captured)
        replay = original(captured['nodes'], captured['state'], {})
        self.assertEqual(replay['review_result']['verdict'], 'pass')
        self.assertEqual(sum(schema == 'ReviewerResult' for schema, _ in FixtureModel.captured), calls_before)

    def test_reviewer_failure_preserves_finalization(self):
        FixtureModel.fail_reviewer = True
        task = self.task()
        self.assertEqual(task.status,'COMPLETED')
        self.assertIn('Reviewer unavailable',self.manager.phase5.store.supervisor(task.id)['metadata']['review_warning'])

    def test_required_reviewer_failure(self):
        self.config['MULTI_AGENT_REVIEW_REQUIRED'] = True
        FixtureModel.fail_reviewer = True
        with self.assertRaises(DelegationRejected): self.task()

    def test_research_cannot_analyze_data(self):
        FixtureModel.bad_tool = 'data.analyze'
        task = self.task('Compare papers')
        record = self.records(task)[0]
        self.assertEqual(record['status'],'FAILED')
        self.assertEqual(self.manager.phase5.store.run(record['agent_run_id'])['tool_calls'],0)

    def test_coding_cannot_call_shell(self):
        FixtureModel.bad_tool = 'shell.execute'
        task = self.task('Review implementation',scope='external')
        self.assertEqual(self.records(task)[0]['status'],'FAILED')

    def test_research_cannot_write_github(self):
        FixtureModel.bad_tool = 'github.issue.create'
        task = self.task('Compare papers')
        self.assertFalse(self.ledger.exists())
        self.assertFalse(self.manager.phase4.actions.approvals(task.id))

    def test_contexts_do_not_include_root_history(self):
        task = self.manager.phase3.task_service.create(self.workspace.id,'Compare papers and experiment data')
        self.manager.store.save_messages(task.session_id,[HumanMessage(content='ROOT_PRIVATE_HISTORY')])
        self.manager.phase3.task_service.run(task.id)
        private = [' '.join(messages) for schema,messages in FixtureModel.captured if schema in {'AgentWorkPlan','ResearchAgentResult','DataAgentResult','ReviewerResult'}]
        self.assertTrue(private)
        self.assertFalse(any('ROOT_PRIVATE_HISTORY' in text for text in private))

    def test_reviewer_sees_no_worker_private_messages(self):
        task = self.task()
        reviewer = next(messages for schema,messages in FixtureModel.captured if schema=='ReviewerResult')
        payload = json.loads(reviewer[-1])
        self.assertNotIn('private_messages',payload)
        self.assertIn('results',payload)

    def test_partial_failure_preserves_successful_sibling(self):
        with patch('server.agent.specialized_agents.PersistentWorkflowNodes._analyze',side_effect=RuntimeError('Data failed')):
            task = self.task()
        records = self.records(task)
        self.assertEqual(next(d for d in records if d['agent_id']=='research')['status'],'COMPLETED')
        self.assertEqual(task.status,'COMPLETED')
        self.assertTrue(self.manager.phase5.store.supervisor(task.id)['metadata']['shared_context']['evidence_ids'])

    def test_timeout_is_partial_failure(self):
        self.config['MULTI_AGENT_AGENT_TIMEOUT_SECONDS'] = .00001
        task = self.task('Compare papers')
        run = self.manager.phase5.store.run(self.records(task)[0]['agent_run_id'])
        self.assertEqual(run['error_type'],'TimeoutError')

    def test_summary_failure_retains_evidence(self):
        FixtureModel.fail_summary = True
        task = self.task('Compare papers')
        record = self.records(task)[0]
        self.assertEqual(record['status'],'PARTIAL')
        self.assertTrue(record['result_json']['evidence_ids'])

    def test_snapshot_stays_stable_for_task(self):
        task = self.task(max_steps=1)
        self.manager.phase5.registry.disable('research')
        resumed = self.manager.phase3.task_service.run(task.id,resume=True)
        self.assertEqual(resumed.status,'COMPLETED')
        self.assertEqual(self.research_count,1)

    def test_restart_preserves_completed_delegations(self):
        task = self.task(max_steps=1)
        self.assertEqual(task.status,'PAUSED')
        previous = self.records(task)
        self.manager = AgentSessionManager(self.config)
        resumed = self.manager.phase3.task_service.run(task.id,resume=True)
        self.assertEqual(resumed.status,'COMPLETED')
        self.assertEqual(self.research_count,1)
        self.assertEqual([d['id'] for d in previous],[d['id'] for d in self.records(task)])

    def test_root_memory_extraction_once(self):
        self.config['MEMORY_ENABLED'] = True
        self.config['MEMORY_AUTO_EXTRACT'] = True
        task = self.task()
        calls = [schema for schema,_ in FixtureModel.captured]
        self.assertEqual(calls.count('MemoryCandidates'),1)
        metadata = self.manager.phase5.store.supervisor(task.id)['metadata']
        self.assertTrue(metadata['memory_extraction_completed'])
        self.assertFalse(self.manager.phase5.store.claim_memory(self.manager.phase5.store.supervisor(task.id)['id']))

    def test_hitl_pause_approve_resume(self):
        FixtureModel.write = True
        task = self.task('Compare papers with experiment data and create an issue in fixture/test')
        self.assertEqual(task.status,'WAITING_USER')
        self.assertFalse(self.ledger.exists())
        approval = self.manager.phase4.actions.approvals(task.id)[0]
        self.manager.phase4.decide(approval['id'],'APPROVED')
        self.assertEqual(self.manager.phase3.tasks.get(task.id).status,'COMPLETED')
        self.assertEqual(len(json.loads(self.ledger.read_text())),1)
        self.assertEqual(self.research_count,1)

    def test_hitl_reject_no_write(self):
        FixtureModel.write = True
        task = self.task('Compare papers with experiment data and create an issue in fixture/test')
        approval = self.manager.phase4.actions.approvals(task.id)[0]
        self.manager.phase4.decide(approval['id'],'REJECTED')
        self.assertFalse(self.ledger.exists())

    def test_hitl_restart_keeps_completed_branches(self):
        FixtureModel.write = True
        task = self.task('Compare papers with experiment data and create an issue in fixture/test')
        approval = self.manager.phase4.actions.approvals(task.id)[0]
        self.manager = AgentSessionManager(self.config)
        self.manager.phase4.registry.register_provider(self.provider)
        asyncio.run(self.manager.phase4.registry.refresh())
        self.manager.phase4.decide(approval['id'],'APPROVED')
        self.assertEqual(self.manager.phase3.tasks.get(task.id).status,'COMPLETED')
        self.assertEqual(self.research_count,1)
        self.assertEqual(len(json.loads(self.ledger.read_text())),1)

    def test_native_parallel_reads_wait_for_all_decisions(self):
        self.config['TOOL_AUTO_APPROVE_READ'] = False
        task = self.task()
        self.assertEqual(task.status,'WAITING_USER')
        approvals = self.manager.phase4.actions.approvals(task.id)
        self.assertGreaterEqual(len(approvals),2)
        self.manager.phase4.decide(approvals[0]['id'],'APPROVED')
        self.assertEqual(self.manager.phase3.tasks.get(task.id).status,'WAITING_USER')
        for _ in range(5):
            pending = [a for a in self.manager.phase4.actions.approvals(task.id) if a['status']=='PENDING']
            if not pending: break
            for approval in pending:
                self.manager.phase4.decide(approval['id'],'APPROVED')
        self.assertEqual(self.manager.phase3.tasks.get(task.id).status,'COMPLETED')

    def test_metrics_usage_unavailable_not_zero(self):
        task = self.task('Compare papers')
        metrics = self.manager.phase5.metrics(self.manager.phase5.store.supervisor(task.id)['id'])
        self.assertIsNone(metrics['total_tokens'])
        self.assertGreater(metrics['total_llm_calls'],0)

    def test_public_trace_does_not_expose_private_state(self):
        task = self.task()
        app = FastAPI()
        register_phase3_api(app,self.manager)
        register_phase4_api(app,self.manager)
        register_phase5_api(app,self.manager)
        client = TestClient(app)
        self.assertEqual(client.get('/agents').status_code,200)
        self.assertEqual(client.get('/agents/imaginary').status_code,404)
        for path in [f'/tasks/{task.id}/agents',f'/tasks/{task.id}/delegations']:
            text = client.get(path).text
            self.assertNotIn('private_messages',text)
            self.assertNotIn('work_plan',text)


if __name__ == '__main__':
    unittest.main()
