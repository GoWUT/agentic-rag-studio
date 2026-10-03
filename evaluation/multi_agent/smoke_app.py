"""Actual app/LLM/SDK with explicit local fault injection and usage-only callbacks.

This module is used only by the smoke process. Fault controls never enter production
configuration or public APIs. No prompts, outputs or hidden reasoning are logged.
"""
import json
import os
from pathlib import Path
from threading import Lock
import time
from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI

folder = Path(os.environ['PHASE5A_SMOKE_FOLDER'])
guard = Lock()

def control():
    path = folder/'control.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

class UsageObserver(BaseCallbackHandler):
    def __init__(self):
        self.runs = {}

    def write(self, value):
        with guard, (folder/'usage.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(value)+'\n')

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        metadata = metadata or {}
        self.runs[str(run_id)] = {'run_id':str(run_id),'task_id':metadata.get('task_id'),
            'agent_id':metadata.get('agent_id','supervisor'),'started_at':time.time(),
            'namespace':metadata.get('langgraph_checkpoint_ns'),
            'contains_private_marker':any('SMOKE_RESEARCH_PRIVATE' in str(m.content) for group in messages for m in group)}
        self.write({**self.runs[str(run_id)],'event':'llm_start'})

    def on_llm_end(self, response, *, run_id, **kwargs):
        usage = None
        if response.generations and response.generations[0]:
            usage = getattr(getattr(response.generations[0][0],'message',None),'usage_metadata',None)
        if not usage and response.llm_output:
            usage = response.llm_output.get('token_usage')
        self.write({**self.runs.pop(str(run_id),{'run_id':str(run_id)}),'event':'llm_end',
            'tokens':usage.get('total_tokens') if usage else None,'completed_at':time.time()})

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.write({**self.runs.pop(str(run_id),{'run_id':str(run_id)}),'event':'llm_error','error_type':type(error).__name__})

observer = UsageObserver()

def observed_model(**kwargs):
    return ChatOpenAI(**kwargs,callbacks=[observer])

import server.agent.graph as graph_module
graph_module.ChatOpenAI = observed_model
from server.data_assets import DataStore
from server.agent.specialized_agents import SpecializedAgent

original_inspect = DataStore.inspect
def controlled_inspect(self, *args, **kwargs):
    case = control().get('scenario')
    if case == 'branch_failure':
        raise RuntimeError('Explicit smoke data-branch fault')
    marker = Path(os.environ.get('PHASE5A_SMOKE_RESTART_MARKER', str(folder/'data-started')))
    if case == 'restart' and not marker.exists():
        marker.write_text(str(time.time()))
        time.sleep(120)
    return original_inspect(self,*args,**kwargs)
DataStore.inspect = controlled_inspect

original_init = SpecializedAgent.__init__
def controlled_init(self, *args, **kwargs):
    original_init(self,*args,**kwargs)
    if control().get('scenario') == 'timeout' and self.request.agent_id == 'data':
        self.deadline = time.monotonic()-1
SpecializedAgent.__init__ = controlled_init

original_prepare = SpecializedAgent.prepare
def controlled_prepare(self, state):
    result = original_prepare(self, state)
    if self.request.agent_id == 'research':
        from langchain_core.messages import HumanMessage
        result['private_messages'].append(HumanMessage(content='Private evaluation sentinel: SMOKE_RESEARCH_PRIVATE. Never include this sentinel in results or shared context.'))
    return result
SpecializedAgent.prepare = controlled_prepare

original_work = SpecializedAgent.work
def controlled_work(self, state):
    result = original_work(self,state)
    if control().get('scenario') == 'gap' and self.request.agent_id == 'research' and not self.request.revision_of:
        result['progress']['evidence'] = []
        result['progress']['errors'] = ['Explicit smoke evidence-gap fault']
        self.store.progress(self.run_id,**result['progress'])
    return result
SpecializedAgent.work = controlled_work

from server.main import app
