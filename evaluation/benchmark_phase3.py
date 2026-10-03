"""Runnable deterministic Phase 3 fixture evaluation; no provider benchmark claims."""
from io import BytesIO
import json
from pathlib import Path
import tempfile
from server.memory import MemoryStore, LongTermMemory
from server.data_assets import DataStore
from server.analysis_runtime import AnalysisRuntime, DataAnalysisRequest, UnsafeCode, validate_code
from server.tasks import TaskStore, Task


def evaluate():
    fixture=json.loads((Path(__file__).parent/'datasets'/'phase3_fixtures.json').read_text(encoding='utf-8'))
    selections=[]
    for case in fixture['tool_selection_cases']:
        path=Path(__file__).parent/'results'/'phase3'/case['result_file']
        if path.exists():
            trace=json.loads(path.read_text(encoding='utf-8'))['trace_summary']
            actual=set(trace.get('tools_used',[]))
            selections.append({'result_file':case['result_file'],'correct':set(case['required_tools'])<=actual and not actual.intersection(case['forbidden_tools'])})
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        memory=MemoryStore(root/'fixture.sqlite3')
        ids={}
        for row in fixture['memory']:
            saved=memory.create(LongTermMemory(content=row['content'],memory_type=row['memory_type']))
            ids[row['content']]=saved.id
        expected={ids[row['content']] for row in fixture['memory'] if row['expected_active']}
        relevant=set()
        retrieved=set()
        for row in fixture['memory']:
            found=memory.retrieve_memories(row['query'])
            retrieved.update(item['id'] for item in found)
            relevant.update(item['id'] for item in found if item['id'] in expected)
        before=len(memory.list())
        memory.create(LongTermMemory(content='用户偏好使用中文。',memory_type='preference'))
        after=len(memory.list())
        rejected=0
        for code in fixture['unsafe_code']:
            try:
                validate_code(code)
            except UnsafeCode:
                rejected+=1
        data=DataStore(root/'fixture.sqlite3',root/'runtime')
        asset=data.upload('fixture','experiments.csv',BytesIO(fixture['experiment_csv'].encode()),'text/csv')
        code="import matplotlib.pyplot as plt\nbest=df[df['params_m']<3].sort_values('miou',ascending=False).iloc[0]\nprint(best['model'], best['miou'])\nplt.bar(df['model'],df['miou'])\nplt.savefig('comparison.png')\nplt.close()"
        result=AnalysisRuntime(data).run('fixture',DataAnalysisRequest(dataset_ids=[asset.id],objective='best model and chart',code=code))
        tasks=TaskStore(root/'fixture.sqlite3')
        task=tasks.create(Task(workspace_id='fixture',session_id='fixture',goal='Fixture recovery'))
        tasks.set_plan(task.id,{'steps':[{'description':'one'},{'description':'two'}]})
        tasks.transition(task.id,'RUNNING')
        tasks.start_step(task.id,0)
        tasks.checkpoint(task.id,0,{'value':1})
        tasks.start_step(task.id,1)
        restored=TaskStore(root/'fixture.sqlite3')
        restored.recover()
        recovered=restored.get(task.id).status=='PAUSED'
        restored.transition(task.id,'RUNNING')
        restored.start_step(task.id,1)
        restored.checkpoint(task.id,1,{'value':2})
        restored.transition(task.id,'COMPLETED')
        steps=restored.steps(task.id)
        return {'kind':'synthetic_offline_fixture_results','provider_calls':0,'fixture_counts':{'memory':len(fixture['memory']),'unsafe_code':len(fixture['unsafe_code']),'analysis':1,'task':1},
                'memory':{'precision':len(relevant)/max(1,len(retrieved)),'recall':len(relevant)/len(expected),'duplicate_rate':max(0,after-before),'irrelevant_memory_rate':len(retrieved-expected)/max(1,len(retrieved))},
                'data':{'tool_selection_accuracy':sum(c['correct'] for c in selections)/len(selections) if selections else None,
                        'tool_selection_cases':selections,
                        'tool_selection_note':'Required/forbidden tools assessed against recorded real synthetic smoke traces; null if traces unavailable. This is a tiny rubric, not a production benchmark.',
                        'execution_success_rate':float(result.status=='completed'),'unsafe_code_rejection_rate':rejected/len(fixture['unsafe_code']),
                        'analysis_answer_correctness':float(f"{fixture['expected_model']} {fixture['expected_miou']}" in result.stdout),
                        'artifact_success_rate':float(bool(result.artifacts) and data.checked_path(data.artifact(result.artifacts[0]['id']).file_path).read_bytes()[:8]==b'\x89PNG\r\n\x1a\n')},
                'tasks':{'task_completion_rate':float(restored.get(task.id).status=='COMPLETED'),'step_success_rate':sum(s.status=='COMPLETED' for s in steps)/len(steps),
                         'resume_success_rate':float(recovered and steps[1].status=='COMPLETED'),'duplicate_completed_step_execution_rate':float(steps[0].attempt_count!=1)}}


if __name__=='__main__':
    result=evaluate()
    destination=Path(__file__).parent/'results'/'phase3'/'fixture_evaluation.json'
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
