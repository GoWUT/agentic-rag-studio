"""Evaluation-only provider/tool fixture around the actual app and worker.

Database, queue, graph, checkpointer, MCP SDK, analysis runner and processes are
real. LLM output and synthetic document retrieval are deterministic and explicitly
identified as fixtures. Controls do not enter production config or public APIs.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from langchain_core.tools import StructuredTool
from server.agent.evidence import Evidence
from test_phase5a import FixtureModel

folder=Path(os.environ['PHASE5B1_SMOKE_FOLDER'])
guard=threading.Lock()


def control():
    path=folder/'control.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def audit(kind, **fields):
    with guard, (folder/'fixture_events.jsonl').open('a',encoding='utf-8') as file:
        file.write(json.dumps({'kind':kind,'at':time.time(),**fields})+'\n')


class RuntimeFixtureModel(FixtureModel):
    def with_structured_output(self,schema,**kwargs):
        if schema.__name__ == 'TaskPlan':
            def invoke(messages):
                objects=self.objects(messages); goal=objects[-1].get('goal','Fixture') if objects else 'Fixture'
                if 'issue' in goal.lower():
                    steps=[{'id':'write','description':'Create local mock issue','query':'Create synthetic issue','sources':[],
                        'preferred_capability':'github.issue.create','arguments':{'owner':'fixture','repo':'test','title':'Phase5B1 synthetic','body':'Local mock only'}}]
                else:
                    steps=[{'id':'read1','description':'Read checkpointed fixture','query':'first '+goal,'sources':['workspace'],'preferred_tool':'search_workspace'},
                           {'id':'read2','description':'Read delayed fixture','query':'second '+goal,'sources':['workspace'],'preferred_tool':'search_workspace'}]
                return schema(goal=goal,steps=steps)
            return SimpleNamespace(invoke=invoke)
        return super().with_structured_output(schema,**kwargs)


def search_workspace(query: str):
    """Read synthetic paper fixture with a controlled delay and durable call audit."""
    settings=control()
    audit('research_call',query=query)
    if query.startswith('second '):
        (folder/'second_started').write_text(str(time.time()))
        time.sleep(float(settings.get('delay',5)))
    return {'evidence':[Evidence(evidence_id='ev_fixture',source_type='workspace',document_id=settings['document_id'],
        document_name='paper.pdf',page=1,page_count=2,
        content='Synthetic method A reaches mIoU 73.27 with 2.5 million parameters.').model_dump()]}


def install_fixture():
    import server.agent.graph as graph
    import server.sessions as sessions
    graph.ChatOpenAI=RuntimeFixtureModel
    sessions.build_tools=lambda *args,**kwargs:[StructuredTool.from_function(search_workspace)]
    original_ask=sessions.AgentSessionManager.ask
    def controlled_ask(self, session_id, message, **kwargs):
        task_id=(kwargs.get('runtime_context') or {}).get('task_id')
        if message.startswith('Transient fixture'):
            marker=folder/('transient-'+task_id)
            if not marker.exists():
                marker.write_text('first read timed out')
                from server.agent.execution_harness import ExecutionUnavailableError
                raise ExecutionUnavailableError('Synthetic transient read failure')
        if message.startswith('Invalid fixture'):
            raise ValueError('Synthetic invalid input')
        return original_ask(self,session_id,message,**kwargs)
    sessions.AgentSessionManager.ask=controlled_ask


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('mode',choices=['api','worker'])
    args=parser.parse_args()
    install_fixture()
    if args.mode=='api':
        import uvicorn
        uvicorn.run('server.main:app',host='127.0.0.1',port=int(os.environ.get('API_PORT','8027')),loop='server.api:selector_loop')
    else:
        from server.worker import main
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(main())
