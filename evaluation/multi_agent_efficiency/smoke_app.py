"""Evaluation-only usage observer and explicit targeted source-gap injection."""
from evaluation.multi_agent import smoke_app as base
from langchain_openai import ChatOpenAI
import time


class EfficiencyObserver(base.UsageObserver):
    def on_llm_end(self,response,*,run_id,**kwargs):
        usage=None
        if response.generations and response.generations[0]:
            usage=getattr(getattr(response.generations[0][0],'message',None),'usage_metadata',None)
        if not usage and response.llm_output:usage=response.llm_output.get('token_usage')
        self.write({**self.runs.pop(str(run_id),{'run_id':str(run_id)}),'event':'llm_end',
            'tokens':usage.get('total_tokens') if usage else None,
            'tokens_prompt':usage.get('input_tokens',usage.get('prompt_tokens')) if usage else None,
            'tokens_completion':usage.get('output_tokens',usage.get('completion_tokens')) if usage else None,
            'completed_at':time.time()})


observer=EfficiencyObserver()
def model(**kwargs):return ChatOpenAI(**kwargs,callbacks=[observer])
base.graph_module.ChatOpenAI=model
original_work=base.SpecializedAgent.work
def targeted_work(self,state):
    result=original_work(self,state)
    if base.control().get('scenario')=='target_gap' and self.request.agent_id=='research' and not self.request.revision_of:
        result['progress']['evidence']=[e for e in result['progress']['evidence'] if e.get('document_name')!='paper_3.pdf']
        result['progress']['errors']=['Required source coverage: paper_3.pdf is missing. Retrieve its reported method, parameters and mIoU only.']
        self.store.progress(self.run_id,**result['progress'])
    return result
base.SpecializedAgent.work=targeted_work
app=base.app

# Evaluation-only controls are read locally. No production endpoint changes.
from server.main import SESSION_MANAGER
original_config=dict(SESSION_MANAGER.config)
@app.middleware('http')
async def evaluation_configuration(request,call_next):
    case=base.control().get('scenario')
    SESSION_MANAGER.config['MULTI_AGENT_ENABLED']=case!='existing_single'
    SESSION_MANAGER.config['MULTI_AGENT_EFFICIENCY_ENABLED']=case!='existing_single'
    SESSION_MANAGER.config['MULTI_AGENT_MAX_LLM_CALLS']=2 if case=='hard_budget' else original_config['MULTI_AGENT_MAX_LLM_CALLS']
    return await call_next(request)
