"""Offline Phase 3 acceptance tests; runner tests execute real subprocesses."""
from io import BytesIO
from pathlib import Path
import json
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
import pandas as pd

from server.memory import LongTermMemory, MemoryCandidate, MemoryStore, sensitive
from server.data_assets import DataStore
from server.analysis_runtime import AnalysisRuntime, DataAnalysisRequest, UnsafeCode, validate_code
from server.tasks import Task, TaskConflict, TaskStore, TaskService
from server.persistence import RecordNotFound
from server.phase3_api import register_phase3_api
from server.sessions import SessionStore
from server.workspaces import WorkspaceStore
from server.agent.context_harness import ContextHarness
from server.agent.evidence import Evidence, render_citations, Citation, validate_citations
from server.agent.graph import build_agent
from server.agent.persistent_workflow import PersistentWorkflowNodes
from test_phase2 import ScriptedModel


CSV = b'model,params_m,miou\nA,2.5,73.27\nB,3.2,76.0\nC,2.0,70.0\n'


class LocalFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'sessions.sqlite3'
        self.sessions = SessionStore(self.path)
        self.workspaces = WorkspaceStore(self.path)
        self.workspace = self.workspaces.create('Phase3 fixture')
        self.config = {'MEMORY_AUTO_EXTRACT':False,'ANALYSIS_TIMEOUT_SECONDS':10,'PLANNER_MAX_REPLAN':0,'GROUNDING_CHECK_ENABLED':False}
        self.memory = MemoryStore(self.path,self.config)
        self.data = DataStore(self.path,self.root/'runtime',self.config)
        self.analysis = AnalysisRuntime(self.data,self.config)
        self.tasks = TaskStore(self.path)
        self.services = SimpleNamespace(memory=self.memory,data=self.data,analysis=self.analysis,tasks=self.tasks)

    def upload(self,content=CSV,name='experiment.csv',mime='text/csv'):
        return self.data.upload(self.workspace.id,name,BytesIO(content),mime)

    def run_code(self,code,config=None,**kwargs):
        asset = self.upload()
        runner = AnalysisRuntime(self.data,{**self.config,**(config or {})})
        return runner.run(self.workspace.id,DataAnalysisRequest(dataset_ids=[asset.id],objective='Analyze experiments',code=code),**kwargs)

    def node(self,script=None,answer='Answer'):
        ScriptedModel.script = script or {}
        ScriptedModel.answer = answer
        return PersistentWorkflowNodes(ScriptedModel(),[],ContextHarness(context_window_tokens=20000,input_budget_tokens=10000,max_output_tokens=512,safety_tokens=512,summary_tokens=256,recent_turns=4),config=self.config,services=self.services)

    def graph(self,query,script,*,state=None,answer='Answer'):
        ScriptedModel.script,ScriptedModel.answer = script,answer
        harness = ContextHarness(context_window_tokens=20000,input_budget_tokens=10000,max_output_tokens=512,safety_tokens=512,summary_tokens=256,recent_turns=4)
        with patch('server.agent.graph.ChatOpenAI',ScriptedModel):
            graph = build_agent('model','key',[],context_harness=harness,max_output_tokens=512,request_timeout_seconds=10,workflow_config=self.config,services=self.services)
        return graph.invoke({'messages':[HumanMessage(content=query)],'workspace_id':self.workspace.id,'source_scope':'workspace_only','document_registry':{},**(state or {})},{'recursion_limit':35})


class MemoryTests(LocalFixture):
    def remember(self,content='User prefers Chinese.',**kwargs):
        return self.memory.create(LongTermMemory(content=content,memory_type='preference',**kwargs))

    def test_crud(self):
        item = self.remember()
        self.assertEqual(self.memory.get(item.id).content,item.content)
        updated = self.memory.update(item.id,'User prefers English.')
        self.assertEqual(self.memory.get(item.id).superseded_by,updated.id)
        self.memory.delete(updated.id)
        self.assertEqual(self.memory.list(),[])

    def test_exact_dedup(self):
        self.assertEqual(self.remember().id,self.remember(' user PREFERS Chinese! ').id)

    def test_cross_language_semantic_preference_dedup(self):
        first = self.remember()
        self.assertEqual(self.remember('用户偏好使用中文。').id,first.id)
        self.assertEqual(self.remember('回答使用中文。').id,first.id)

    def test_embedding_semantic_dedup(self):
        self.memory.embedder = Mock(embed_query=lambda text:[1.,0.])
        first = self.memory.create(LongTermMemory(content='Use hybrid retrieval in this project.',memory_type='decision'))
        second = self.memory.create(LongTermMemory(content='Combining sparse and dense search was approved.',memory_type='decision'))
        self.assertEqual(first.id,second.id)

    def test_conflict_excludes_superseded(self):
        old = self.remember('User prefers concise responses.')
        new = self.remember('User now prefers detailed responses.')
        self.assertEqual(self.memory.get(old.id).status,'superseded')
        self.assertEqual(self.memory.get(old.id).superseded_by,new.id)
        self.assertEqual([m['id'] for m in self.memory.retrieve_memories('preferred response format')],[new.id])

    def test_decision_conflict_key(self):
        old = self.memory.create(LongTermMemory(content='Use backend A.',memory_type='decision',metadata={'conflict_key':'backend'}))
        new = self.memory.create(LongTermMemory(content='Use backend B.',memory_type='decision',metadata={'conflict_key':'backend'}))
        self.assertEqual(self.memory.get(old.id).superseded_by,new.id)

    def test_workspace_scope(self):
        other = self.workspaces.create('other')
        item = self.remember('Project prefers concise answers.',scope_type='workspace',scope_id=other.id)
        self.assertEqual(self.memory.list(workspace_id=self.workspace.id),[])
        self.assertEqual(self.memory.list(workspace_id=other.id)[0].id,item.id)

    def test_owner_scope(self):
        self.remember(owner_id='other_owner')
        self.assertEqual(self.memory.retrieve_memories('Chinese'),[])

    def test_user_memory_cross_workspace(self):
        item = self.remember()
        self.assertEqual(self.memory.list(workspace_id='another')[0].id,item.id)

    def test_token_budget_and_topk(self):
        self.memory.config.update(MEMORY_TOKEN_BUDGET=120,MEMORY_MAX_ITEMS=2)
        for index in range(8):
            self.memory.create(LongTermMemory(content='Always follow instruction '+str(index)+'x'*50,memory_type='instruction'))
        results = self.memory.retrieve_memories('instruction')
        self.assertLessEqual(len(results),2)
        self.assertLessEqual(sum(len(json.dumps(m,ensure_ascii=False)) for m in results),240)

    def test_cross_session_and_restart(self):
        item = self.remember('Preferred report format is markdown.')
        restored = MemoryStore(self.path)
        self.assertEqual(restored.retrieve_memories('report format')[0]['id'],item.id)
        self.assertEqual(restored.get(item.id).access_count,1)

    def test_secret_rejection(self):
        for value in ['api_key: sk-1234567890','password is hunter123','token abcdefghijk','authorization: Bearer secret','-----BEGIN RSA PRIVATE KEY-----','11010519491231002X','4111 1111 1111 1111']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.remember(value)
        self.assertEqual(self.memory.list(),[])

    def test_low_confidence_and_importance_rejected(self):
        for confidence,importance in [(.1,.9),(.9,.1)]:
            record = LongTermMemory(content='Do something.',memory_type='instruction',confidence=confidence,importance=importance)
            self.assertIsNone(self.memory.create(record,automatic=True))

    def test_explicit_remember_extraction(self):
        node = self.node()
        result = node.extract_memory({'original_query':'Remember my preferred report format is markdown.','final_answer':'Remembered','workspace_id':self.workspace.id})
        self.assertEqual(result['memory_written'],1)
        self.assertIn('markdown',self.memory.retrieve_memories('format')[0]['content'])

    def test_structured_auto_extraction(self):
        self.config['MEMORY_AUTO_EXTRACT'] = True
        node = self.node({'MemoryCandidates':[{'candidates':[{'should_store':True,'memory_type':'decision','content':'Project uses hybrid retrieval.','scope_type':'workspace','importance':.9,'confidence':.9}]}]})
        node.extract_memory({'original_query':'We decided to use hybrid retrieval.','workspace_id':self.workspace.id})
        self.assertEqual(self.memory.list(workspace_id=self.workspace.id)[0].memory_type,'decision')

    def test_small_talk_does_not_call_extractor(self):
        self.config['MEMORY_AUTO_EXTRACT'] = True
        node = self.node()
        node.extract_memory({'original_query':'今天天气不错','workspace_id':self.workspace.id})
        self.assertEqual(self.memory.list(),[])
        self.assertEqual(ScriptedModel.calls['MemoryCandidates'],0)

    def test_extraction_parse_failure_is_nonfatal(self):
        self.config['MEMORY_AUTO_EXTRACT'] = True
        node = self.node({'MemoryCandidates':[ValueError('invalid')]})
        self.assertEqual(node.extract_memory({'original_query':'We decided this project uses hybrid retrieval.'})['memory_written'],0)

    def test_sensitive_extraction_skips_model(self):
        self.config['MEMORY_AUTO_EXTRACT'] = True
        node = self.node()
        node.extract_memory({'original_query':'Remember api_key: sk-1234567890'})
        self.assertEqual(ScriptedModel.calls['MemoryCandidates'],0)

    def test_memory_db_failure_is_nonfatal_to_retrieval(self):
        node = self.node()
        with patch.object(self.memory,'retrieve_memories',side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(node.retrieve_memory({'messages':[HumanMessage(content='hello')]}),{'retrieved_memories':[]})

    def test_memory_never_becomes_evidence(self):
        self.remember('Preferred report format is markdown.')
        result = self.graph('What is my preferred report format?',{'QueryAnalysis':[{'standalone_query':'What is my preferred report format?','query_type':'direct','needs_retrieval':False,'selected_sources':[]}]})
        self.assertEqual(result['evidence'],[])
        self.assertEqual(result['citations'],[])
        self.assertIn('markdown',result['retrieved_memories'][0]['content'])

    def test_update_failure_preserves_active_memory(self):
        item = self.remember()
        with self.assertRaises(ValueError):
            self.memory.update(item.id,'password: do-not-save')
        self.assertEqual(self.memory.get(item.id).status,'active')


class DatasetTests(LocalFixture):
    def test_csv_upload_and_inspect(self):
        asset = self.upload()
        inspected = self.data.inspect(self.workspace.id,asset.id)['sheets']['data']
        self.assertEqual(inspected['rows'],3)
        self.assertEqual(inspected['columns'],3)
        self.assertEqual(inspected['missing']['miou'],0)
        self.assertIn('miou',inspected['statistics'])

    def test_xlsx_multi_sheet(self):
        stream = BytesIO()
        with pd.ExcelWriter(stream,engine='openpyxl') as writer:
            pd.read_csv(BytesIO(CSV)).to_excel(writer,index=False,sheet_name='Results')
            pd.DataFrame({'x':[1,2]}).to_excel(writer,index=False,sheet_name='Other')
        asset = self.upload(stream.getvalue(),'experiment.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertEqual(asset.schema_metadata['sheet_names'],['Results','Other'])
        self.assertEqual(asset.schema_metadata['sheets']['Other']['rows'],2)

    def test_json_records(self):
        asset = self.upload(b'[{"x":1},{"x":2}]','data.json','application/json')
        self.assertEqual(asset.row_count,2)

    def test_json_tabular_object(self):
        asset = self.upload(b'{"x":[1,2],"y":[3,4]}','data.json','application/json')
        self.assertEqual(asset.column_count,2)

    def test_json_unsupported_shape(self):
        with self.assertRaisesRegex(ValueError,'Unsupported JSON shape'):
            self.upload(b'{"nested":{"a":1}}','data.json','application/json')

    def test_invalid_json(self):
        with self.assertRaises(ValueError):
            self.upload(b'not json','data.json','application/json')

    def test_corrupt_excel(self):
        with self.assertRaises(ValueError):
            self.upload(b'not zip','data.xlsx')

    def test_csv_encoding_error(self):
        with self.assertRaises(ValueError):
            self.upload(b'x\n\xff\xfe\x80')

    def test_executable_and_mime_rejected(self):
        for filename,mime in [('bad.py','text/plain'),('bad.exe','application/octet-stream'),('a.csv','application/x-msdownload')]:
            with self.assertRaises(ValueError):
                self.upload(CSV,filename,mime)

    def test_upload_limit(self):
        self.data.config['DATASET_MAX_FILE_MB'] = 1
        with self.assertRaises(ValueError):
            self.upload(b'a'*(1024*1024+1))

    def test_dedup_and_delete(self):
        first = self.upload()
        self.assertEqual(first.id,self.upload().id)
        self.data.delete(self.workspace.id,first.id)
        self.assertEqual(self.data.list(self.workspace.id),[])
        self.assertTrue(Path(first.file_path).is_file())

    def test_dataset_scope_and_path_isolation(self):
        asset = self.upload()
        with self.assertRaises(RecordNotFound):
            self.data.get('other',asset.id)
        with self.assertRaises(ValueError):
            self.data.checked_path(self.path)


class SecurityTests(LocalFixture):
    def test_forbidden_imports(self):
        for module in ['os','subprocess','socket','requests','urllib','http','ctypes','shutil','multiprocessing','openpyxl']:
            with self.subTest(module=module),self.assertRaises(UnsafeCode):
                validate_code('import '+module)

    def test_forbidden_builtins(self):
        for function in ['eval','exec','compile','__import__','globals','locals','getattr','input','breakpoint','open']:
            with self.subTest(function=function),self.assertRaises(UnsafeCode):
                validate_code(function+'("x")')

    def test_dunder_exploit(self):
        for code in ['object.__subclasses__()','df.__class__.__mro__','print(df._mgr)']:
            with self.assertRaises(UnsafeCode):
                validate_code(code)

    def test_absolute_paths_and_traversal(self):
        for path in ['C:/Windows/win.ini','/etc/passwd','../x.csv','..\\x.csv','https://example.com/x.csv']:
            with self.assertRaises(UnsafeCode):
                validate_code(f'df.to_csv({path!r})')

    def test_all_host_read_paths_rejected(self):
        for code in ['import pandas as pd\npd.read_csv("host.csv")','import numpy as np\nnp.load("host.npy")','df.to_html("host.html")','df.to_pickle("a.pkl")','df.query("@x")','df.eval("x")']:
            with self.assertRaises(UnsafeCode):
                validate_code(code)

    def test_library_module_escape_rejected(self):
        for code in ["import collections\ncollections.sys.modules['subprocess'].run(['cmd'])",
                     'from collections import sys',
                     "df.style.env.globals['open']('host.txt')",
                     "import matplotlib.pyplot as plt\nfig=plt.figure()\nfig.canvas.print_png('../host.png')"]:
            with self.subTest(code=code),self.assertRaises(UnsafeCode):
                validate_code(code)

    def test_dynamic_artifact_path_rejected(self):
        with self.assertRaises(UnsafeCode):
            validate_code('filename="x.csv"\ndf.to_csv(filename)')

    def test_unsafe_code_never_starts_subprocess(self):
        with patch('server.analysis_runtime.subprocess.Popen') as process:
            result = self.run_code('import socket')
        process.assert_not_called()
        self.assertEqual(result.error_type,'UnsafeCode')

    def test_code_length_limit(self):
        result = self.run_code('print(1)'*100,{'ANALYSIS_MAX_CODE_CHARS':50})
        self.assertEqual(result.error_type,'UnsafeCode')

    def test_safe_pandas_analysis(self):
        result = self.run_code("best = df[df['params_m'] < 3].sort_values('miou',ascending=False).iloc[0]\nprint(best['model'],best['miou'])")
        self.assertEqual(result.status,'completed',result.stderr)
        self.assertIn('A 73.27',result.stdout)

    def test_matplotlib_real_png_and_artifact(self):
        result = self.run_code("import matplotlib.pyplot as plt\nplt.bar(df['model'],df['miou'])\nplt.text(0,73.27,'73.27')\nplt.savefig('comparison.png')\nplt.close()")
        self.assertEqual(result.status,'completed',result.stderr)
        artifact = self.data.artifact(result.artifacts[0]['id'])
        self.assertEqual(Path(artifact.file_path).read_bytes()[:8],b'\x89PNG\r\n\x1a\n')

    def test_csv_json_artifacts(self):
        result = self.run_code("df.to_csv('summary.csv',index=False)\ndf.to_json('summary.json',orient='records')")
        self.assertEqual(result.status,'completed',result.stderr)
        self.assertEqual(len(result.artifacts),2)

    def test_guarded_json_and_text_writer(self):
        result = self.run_code("import json\nwrite_artifact('summary.json',json.dumps({'rows':len(df)}))\nwrite_artifact('report.txt','three rows')")
        self.assertEqual(result.status,'completed',result.stderr)
        self.assertEqual(len(result.artifacts),2)

    def test_guarded_writer_rejects_host_paths(self):
        with self.assertRaises(UnsafeCode):
            validate_code("write_artifact('C:/outside.txt','unsafe')")

    def test_numeric_conversion_and_numpy_reductions(self):
        result = self.run_code("import pandas as pd\nimport numpy as np\nvalues=pd.to_numeric(df['miou']).to_numpy(dtype=float)\nprint(values.std(ddof=0))")
        self.assertEqual(result.status,'completed',result.stderr)
        self.assertTrue(result.stdout.strip())

    def test_loop_placeholder_is_not_dunder_access(self):
        result=self.run_code("for _, row in df.iterrows():\n    print(row['model'])")
        self.assertEqual(result.status,'completed',result.stderr)

    def test_timeout_kills_child(self):
        result = self.run_code('while True: continue',{'ANALYSIS_TIMEOUT_SECONDS':1})
        self.assertEqual(result.error_type,'ExecutionTimeout')
        self.assertLess(result.duration_ms,6000)

    def test_stdout_truncated(self):
        result = self.run_code("print('x'*100000)",{'ANALYSIS_MAX_OUTPUT_CHARS':1000})
        self.assertEqual(result.status,'completed',result.stderr)
        self.assertEqual(len(result.stdout),1000)

    def test_artifact_count_limit(self):
        result = self.run_code("df.to_csv('one.csv')\ndf.to_csv('two.csv')",{'ANALYSIS_MAX_ARTIFACTS':1})
        self.assertEqual(result.error_type,'ArtifactLimitExceeded')
        self.assertEqual(result.artifacts,[])

    def test_artifact_size_limit(self):
        self.config['ANALYSIS_MAX_ARTIFACT_MB']=.00001
        result = self.run_code("df.to_csv('large.csv')")
        self.assertEqual(result.error_type,'ArtifactLimitExceeded')

    def test_name_error_result(self):
        result = self.run_code('print(missing_name)')
        self.assertEqual(result.error_type,'NameError')

    def test_syntax_error_result(self):
        result = self.run_code('if')
        self.assertEqual(result.error_type,'SyntaxError')

    def test_invalid_artifact_id(self):
        with self.assertRaises(RecordNotFound):
            self.data.artifact('missing')

    def test_task_artifact_lineage(self):
        result = self.run_code("df.to_csv('result.csv')",task_id='task',step_id='step')
        artifact = self.data.artifact(result.artifacts[0]['id'])
        self.assertEqual((artifact.task_id,artifact.step_id,artifact.execution_id),('task','step',result.execution_id))

    def test_analysis_citation_lineage(self):
        evidence = Evidence(evidence_id='ev_data',source_type='analysis',execution_id='run',dataset_ids=['dataset'],content='A 73.27')
        answer,citations = render_citations('A has 73.27 [ev_data]',[evidence])
        self.assertIn('[Analysis run]',answer)
        self.assertIsNone(citations[0].page)
        self.assertEqual(citations[0].dataset_ids,['dataset'])

    def test_forged_analysis_lineage_rejected(self):
        evidence = Evidence(evidence_id='ev_data',source_type='analysis',execution_id='real',content='result')
        self.assertEqual(validate_citations([Citation(citation_id='1',evidence_id='ev_data',execution_id='fake')],[evidence]),[])

    def test_syntax_error_repair_once(self):
        asset = self.upload()
        node = self.node({'GeneratedCode':[{'code':'if'},{'code':"print(len(df))"}]})
        result = node._analyze({'workspace_id':self.workspace.id,'data_asset_ids':[asset.id]},'calculate results')
        self.assertEqual(result['status'],'completed',result['stderr'])
        self.assertEqual(len(result['execution_attempts']),2)

    def test_repair_max_limit(self):
        asset = self.upload()
        node = self.node({'GeneratedCode':[{'code':'if'},{'code':'if'}]})
        result = node._analyze({'workspace_id':self.workspace.id,'data_asset_ids':[asset.id]},'calculate results')
        self.assertEqual(result['error_type'],'SyntaxError')
        self.assertEqual(ScriptedModel.calls['GeneratedCode'],2)

    def test_structural_question_without_code_generation(self):
        asset = self.upload()
        node = self.node()
        result = node._analyze({'workspace_id':self.workspace.id,'data_asset_ids':[asset.id]},'How many rows?')
        self.assertIn('3',result['stdout'])
        self.assertEqual(ScriptedModel.calls['GeneratedCode'],0)

    def test_simple_mean_without_code_generation(self):
        asset = self.upload()
        result = self.node()._analyze({'workspace_id':self.workspace.id,'data_asset_ids':[asset.id]},'What is the average miou?')
        self.assertEqual(result['status'],'completed',result['stderr'])
        self.assertEqual(ScriptedModel.calls['GeneratedCode'],0)

    def test_subprocess_start_failure_is_reported(self):
        with patch('server.analysis_runtime.subprocess.Popen',side_effect=OSError('private host detail')):
            result = self.run_code('print(len(df))')
        self.assertEqual(result.error_type,'ExecutionUnavailable')
        self.assertNotIn('private',result.stderr)

    def test_artifact_write_failure(self):
        with patch.object(self.data,'register_artifact',side_effect=OSError('write failure')):
            result = self.run_code("df.to_csv('summary.csv')")
        self.assertEqual(result.error_type,'ArtifactWriteFailure')

    def test_missing_dependency(self):
        # Simulate the worker missing a development dependency, not an install attempt.
        with patch('server.analysis_runtime.subprocess.Popen') as spawn:
            child=spawn.return_value
            child.stdout=BytesIO()
            child.stderr=BytesIO(b"ModuleNotFoundError: No module named 'pandas'")
            child.poll.return_value=1
            child.returncode=1
            child.wait.return_value=1
            result=self.run_code('print(len(df))')
        self.assertEqual(result.error_type,'DependencyUnavailable')


class TaskTests(LocalFixture):
    def task(self):
        return self.tasks.create(Task(workspace_id=self.workspace.id,session_id='session',goal='Analyze experiments'))

    def plan(self,task):
        self.tasks.set_plan(task.id,{'goal':task.goal,'steps':[{'description':'first','query':'first','sources':[],'preferred_tool':'inspect_dataset'},
                                                            {'description':'second','query':'second','sources':[],'preferred_tool':'analyze_data'}]})

    def test_task_creation_and_relations(self):
        task = self.task()
        self.assertEqual(task.status,'PENDING')
        self.assertEqual(self.tasks.list(self.workspace.id)[0].session_id,'session')

    def test_plan_and_steps_persist(self):
        task = self.task()
        self.plan(task)
        self.assertEqual(len(TaskStore(self.path).steps(task.id)),2)
        self.assertIsNotNone(self.tasks.get(task.id).plan_json)

    def test_running_completed_transitions(self):
        task = self.task()
        self.tasks.transition(task.id,'RUNNING')
        completed = self.tasks.transition(task.id,'COMPLETED')
        self.assertIsNotNone(completed.started_at)
        self.assertIsNotNone(completed.completed_at)

    def test_pause_resume_cancel(self):
        task = self.task()
        for status in ['RUNNING','PAUSED','RUNNING','CANCELLED']:
            self.assertEqual(self.tasks.transition(task.id,status).status,status)

    def test_completed_step_not_repeated(self):
        task = self.task()
        self.plan(task)
        self.tasks.transition(task.id,'RUNNING')
        self.tasks.start_step(task.id,0)
        self.tasks.checkpoint(task.id,0,{'evidence':['saved']})
        self.assertIsNone(self.tasks.start_step(task.id,0))
        self.assertEqual(self.tasks.steps(task.id)[0].attempt_count,1)

    def test_backend_restart_recovery(self):
        task = self.task()
        self.plan(task)
        self.tasks.transition(task.id,'RUNNING')
        self.tasks.start_step(task.id,0)
        self.tasks.checkpoint(task.id,0,{'result':'saved'})
        self.tasks.start_step(task.id,1)
        restored = TaskStore(self.path)
        restored.recover()
        self.assertEqual(restored.get(task.id).status,'PAUSED')
        self.assertEqual([s.status for s in restored.steps(task.id)],['COMPLETED','PENDING'])
        self.assertEqual(restored.events(task.id)[-1].event_type,'TASK_RECOVERED_AFTER_RESTART')

    def test_cancel_stops_scheduling(self):
        task = self.task()
        self.plan(task)
        self.tasks.transition(task.id,'RUNNING')
        self.tasks.transition(task.id,'CANCELLED')
        self.assertIsNone(self.tasks.start_step(task.id,0))

    def test_waiting_user(self):
        task = self.task()
        self.tasks.transition(task.id,'RUNNING')
        self.assertEqual(self.tasks.transition(task.id,'WAITING_USER').status,'WAITING_USER')
        self.assertEqual(self.tasks.transition(task.id,'RUNNING').status,'RUNNING')

    def test_failed_step_and_max_attempts(self):
        task = self.task()
        self.plan(task)
        self.tasks.transition(task.id,'RUNNING')
        for attempt in range(2):
            self.tasks.start_step(task.id,0)
            self.tasks.checkpoint(task.id,0,{},success=False)
        with self.assertRaises(TaskConflict):
            self.tasks.start_step(task.id,0)

    def test_append_only_events(self):
        task = self.task()
        before = self.tasks.events(task.id)
        self.tasks.transition(task.id,'RUNNING')
        after = self.tasks.events(task.id)
        self.assertEqual(before,after[:len(before)])
        self.assertEqual(len(after),2)

    def test_conflicting_transition(self):
        task = self.task()
        self.tasks.transition(task.id,'RUNNING')
        with self.assertRaises(TaskConflict):
            self.tasks.transition(task.id,'RUNNING')

    def test_checkpoint_committed_before_next_step(self):
        task = self.task()
        self.plan(task)
        self.tasks.transition(task.id,'RUNNING')
        self.tasks.start_step(task.id,0)
        self.tasks.checkpoint(task.id,0,{'result':42})
        self.assertEqual(TaskStore(self.path).steps(task.id)[0].output_json,{'result':42})

    def test_recovery_is_idempotent(self):
        task = self.task()
        self.tasks.transition(task.id,'RUNNING')
        self.tasks.recover()
        count = len(self.tasks.events(task.id))
        self.tasks.recover()
        self.assertEqual(len(self.tasks.events(task.id)),count)

    def test_completed_task_extraction(self):
        self.config['MEMORY_AUTO_EXTRACT'] = True
        task = self.task()
        self.tasks.transition(task.id,'RUNNING')
        node = self.node({'MemoryCandidates':[{'candidates':[{'should_store':True,'memory_type':'episodic','content':'Experiment fixture analysis completed.','scope_type':'workspace','importance':.9,'confidence':.9}]}]})
        node.extract_memory({'task_id':task.id,'original_query':task.goal,'final_answer':'A won.','workspace_id':self.workspace.id})
        self.assertEqual(self.memory.list(workspace_id=self.workspace.id)[0].source_task_id,task.id)


class IntegrationTests(LocalFixture):
    def test_resume_after_last_checkpoint_skips_executor(self):
        self.upload()
        task=self.tasks.create(Task(workspace_id=self.workspace.id,session_id='s',goal='Analyze dataset'))
        self.tasks.set_plan(task.id,{'goal':task.goal,'steps':[{'id':'synthesize','description':'report','query':task.goal,'sources':[],'preferred_tool':'synthesize'}]})
        self.tasks.transition(task.id,'RUNNING')
        self.tasks.start_step(task.id,0)
        output={'final_answer':'Already verified report','synthesis_completed':True,'verification_result':None,'evidence':[],'evidence_pool':[],'selected_sources':[],'step_results':[{'step':0,'status':'completed'}]}
        self.tasks.checkpoint(task.id,0,output)
        self.tasks.transition(task.id,'PAUSED')
        self.tasks.transition(task.id,'RUNNING')
        plan=self.tasks.get(task.id).plan_json
        restored={**output,'task_plan':plan,'plan':plan['steps'],'current_step':1,'plan_is_task_plan':True,'original_query':task.goal,'standalone_query':task.goal,'task_goal':task.goal,'task_complexity':'complex','needs_retrieval':True,'query_type':'research'}
        result=self.graph(task.goal,{},state={'task_id':task.id,'task_resume':restored})
        self.assertEqual(result['final_answer'],'Already verified report')
        self.assertEqual(self.tasks.steps(task.id)[0].attempt_count,1)

    def test_checkpointed_synthesis_preserves_public_citations(self):
        node=self.node()
        citation={'citation_id':'1','evidence_id':'ev_pdf','document_name':'paper.pdf','page':1}
        result=node.citation_validator({'synthesis_completed':True,'final_answer':'Supported [paper.pdf, p.1]',
                                        'citations':[citation],'trace_summary':{'citation_count':1}})
        self.assertEqual(result['citations'],[citation])
        self.assertIn('[paper.pdf, p.1]',result['final_answer'])

    def test_manager_construction_does_not_recover_running_task(self):
        from server.phase3 import Phase3Services
        task=self.tasks.create(Task(workspace_id=self.workspace.id,session_id='s',goal='live task'))
        self.tasks.transition(task.id,'RUNNING')
        manager=SimpleNamespace(config={},store=self.sessions,workspace=self.root)
        Phase3Services(manager)
        self.assertEqual(self.tasks.get(task.id).status,'RUNNING')
    def test_task_missing_dataset_waits_for_user(self):
        task=self.tasks.create(Task(workspace_id=self.workspace.id,session_id='s',goal='Analyze missing.xlsx'))
        self.tasks.transition(task.id,'RUNNING')
        result=self.graph(task.goal,{},state={'task_id':task.id})
        self.assertEqual(self.tasks.get(task.id).status,'WAITING_USER')
        self.assertIn('WAITING_USER',result['final_answer'])

    def test_named_dataset_selection(self):
        csv=self.upload()
        other=self.upload(b'x\n1\n','other.csv')
        query='How many rows are in experiment.csv?'
        node=self.node({'QueryAnalysis':[{'standalone_query':query,'query_type':'direct','needs_retrieval':False,'selected_sources':[]}]})
        result=node.query_analyzer({'workspace_id':self.workspace.id,'messages':[HumanMessage(content=query)]})
        self.assertEqual(result['data_asset_ids'],[csv.id])
    def test_data_graph_analysis_evidence_and_real_chart(self):
        asset = self.upload()
        query = 'Analyze experiment.csv, choose best mIoU under 3M parameters and generate chart.'
        script = {'QueryAnalysis':[{'standalone_query':query,'query_type':'direct','needs_retrieval':False,'selected_sources':[]}],
                  'TaskPlan':[{'goal':query,'steps':[{'id':'1','description':'Inspect','query':query,'sources':[],'preferred_tool':'inspect_dataset'},
                                                     {'id':'2','description':'Analyze','query':query,'sources':[],'preferred_tool':'analyze_data'}]}],
                  'GeneratedCode':[{'code':"import matplotlib.pyplot as plt\nbest=df[df['params_m']<3].sort_values('miou',ascending=False).iloc[0]\nprint(best['model'],best['miou'])\nplt.bar(df['model'],df['miou'])\nplt.savefig('comparison.png')\nplt.close()"}],
                  'RetrievalGrade':[{'relevant':True,'sufficient':True,'confidence':1,'reason':'computed'}]}
        result = self.graph(query,script)
        self.assertIn('A 73.27',result['analysis_result']['stdout'])
        self.assertEqual(result['evidence'][0]['source_type'],'analysis')
        self.assertEqual(result['evidence'][0]['dataset_ids'],[asset.id])
        self.assertIn('experiment.csv',result['evidence'][0]['content'])
        self.assertEqual(result['artifacts'][0]['mime_type'],'image/png')

    def test_task_pause_restart_resume_next_step(self):
        self.upload()
        task = self.tasks.create(Task(workspace_id=self.workspace.id,session_id='s',goal='Analyze dataset'))
        self.tasks.transition(task.id,'RUNNING')
        query=task.goal
        script={'QueryAnalysis':[{'standalone_query':query,'query_type':'direct','needs_retrieval':False,'selected_sources':[]}],
                'TaskPlan':[{'goal':query,'steps':[{'id':'1','description':'Inspect','query':query,'sources':[],'preferred_tool':'inspect_dataset'},
                                                   {'id':'2','description':'Analyze','query':query,'sources':[],'preferred_tool':'analyze_data'}]}]}
        paused = self.graph(query,script,state={'task_id':task.id,'task_step_limit':1})
        self.assertEqual(self.tasks.get(task.id).status,'PAUSED')
        self.assertEqual(self.tasks.steps(task.id)[0].status,'COMPLETED')
        restored = TaskStore(self.path)
        restored.recover()
        restored.transition(task.id,'RUNNING')
        plan=restored.get(task.id).plan_json
        saved=restored.steps(task.id)[0].output_json
        resume={**saved,'task_plan':plan,'plan':plan['steps'],'current_step':1,'plan_is_task_plan':True,'original_query':query,'standalone_query':query,'task_goal':query,'task_complexity':'complex','needs_retrieval':True,'query_type':'research'}
        result=self.graph(query,{'GeneratedCode':[{'code':'print(len(df))'}]},state={'task_id':task.id,'task_resume':resume})
        self.assertEqual([s.status for s in restored.steps(task.id)],['COMPLETED','COMPLETED','COMPLETED'])
        self.assertEqual(restored.steps(task.id)[0].attempt_count,1)
        self.assertEqual(ScriptedModel.calls['TaskPlan'],0)
        self.assertEqual(ScriptedModel.calls['QueryAnalysis'],0)

    def test_existing_tables_and_messages_survive_migration(self):
        self.sessions.create(session_id='legacy',file_id='f',file_name='f.pdf',pdf_path='pdf',chroma_dir='chroma',embedding_model='model')
        self.sessions.save_messages('legacy',[HumanMessage(content='existing')])
        MemoryStore(self.path)
        DataStore(self.path,self.root/'runtime')
        TaskStore(self.path)
        self.assertEqual(SessionStore(self.path).load_messages('legacy')[0].content,'existing')
        self.assertEqual(WorkspaceStore(self.path).get(self.workspace.id).name,'Phase3 fixture')

    def test_model_initialization_is_shared_across_threads(self):
        from server.rag import embeddings
        with patch.object(embeddings,'_EMBEDDERS',{}),patch.object(embeddings,'HuggingFaceEmbeddings',return_value=object()) as factory:
            with ThreadPoolExecutor(max_workers=4) as executor:
                values=list(executor.map(embeddings.get_embedder,['same']*4))
            factory.assert_called_once()
            self.assertTrue(all(value is values[0] for value in values))


class APITests(LocalFixture):
    def setUp(self):
        super().setUp()
        manager=SimpleNamespace(phase3=self.services,config=self.config,workspaces=self.workspaces)
        self.app=FastAPI()
        register_phase3_api(self.app,manager)
        self.client=TestClient(self.app)

    def test_memory_crud_api(self):
        created=self.client.post('/memories',json={'content':'Preferred report format is markdown.','memory_type':'preference'}).json()
        self.assertEqual(len(self.client.get('/memories').json()),1)
        updated=self.client.patch('/memories/'+created['id'],json={'content':'User prefers detailed responses.'})
        self.assertEqual(updated.status_code,200)
        self.assertEqual(self.client.delete('/memories/'+updated.json()['id']).status_code,204)

    def test_sensitive_memory_api(self):
        self.assertEqual(self.client.post('/memories',json={'content':'api_key: sk-1234567890'}).status_code,400)

    def test_dataset_api_and_scope(self):
        url=f'/workspaces/{self.workspace.id}/datasets'
        uploaded=self.client.post(url,files={'file':('x.csv',CSV,'text/csv')})
        self.assertEqual(uploaded.status_code,200)
        self.assertNotIn('file_path',uploaded.json())
        self.assertEqual(self.client.get(url+'/'+uploaded.json()['id']).status_code,200)
        self.assertEqual(self.client.delete(url+'/'+uploaded.json()['id']).status_code,204)

    def test_artifact_api_is_id_based(self):
        result=self.run_code("df.to_csv('summary.csv')")
        response=self.client.get('/artifacts/'+result.artifacts[0]['id'])
        self.assertEqual(response.status_code,200)
        self.assertIn('attachment',response.headers['content-disposition'])
        self.assertEqual(self.client.get('/artifacts/missing?path=C:/Windows/win.ini').status_code,404)

    def test_unsafe_analysis_api(self):
        asset=self.upload()
        response=self.client.post(f'/workspaces/{self.workspace.id}/analysis',json={'dataset_ids':[asset.id],'objective':'unsafe','code':'import os'})
        self.assertEqual(response.json()['error_type'],'UnsafeCode')

    def test_database_error_api(self):
        with patch.object(self.memory,'list',side_effect=sqlite3.OperationalError('private path')):
            response=self.client.get('/memories')
        self.assertEqual(response.status_code,503)
        self.assertNotIn('private',response.text)

    def test_task_list_detail_events(self):
        task=self.tasks.create(Task(workspace_id=self.workspace.id,session_id='s',goal='test'))
        self.assertEqual(self.client.get('/tasks/'+task.id).status_code,200)
        self.assertEqual(self.client.get('/tasks/'+task.id+'/events').json()[0]['event_type'],'TASK_CREATED')
        self.assertEqual(self.client.post('/tasks/'+task.id+'/cancel').json()['status'],'CANCELLED')


if __name__ == '__main__':
    unittest.main()
