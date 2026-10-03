"""Repository access boundary. Existing domain logic stays in domain stores."""
from functools import wraps
from fastapi import HTTPException
from server.auth.context import require_principal
from server.auth.store import AccountStore
from server.auth.service import AuthService
from server.auth.authorization import AuthorizationService


class ScopedRepository:
    def __init__(self,repository,security,domain):
        self.repository,self.security,self.domain=repository,security,domain

    def __getattr__(self,name):
        value=getattr(self.repository,name)
        if not callable(value) or name in {'connect','_connect','checked_path','public_run','public_delegation','recover','_save','_save_run','_get','_event'}:
            return value
        @wraps(value)
        def call(*args,**kwargs):
            principal=require_principal(); security=self.security
            first=args[0] if args else None
            if self.domain=='workspace':
                if name=='create':return security.create_workspace(*args,**kwargs,principal=principal)
                if name=='list':return security.visible(principal,'workspace.read',value(*args,**kwargs))
                action={'get':'workspace.read','delete':'workspace.delete','documents':'document.read',
                        'register':'document.create','delete_document':'document.delete'}.get(name)
                if not action:raise HTTPException(403,'Operation unavailable')
                security.authorize(principal,action,first.workspace_id if name=='register' else first)
                if name=='delete':
                    # Keep historical resources; reject nonempty workspace deletion.
                    with security.store.connect() as db:
                        for table in ('sessions','tasks','data_assets'):
                            if db.execute('SELECT 1 FROM '+table+' WHERE workspace_id=?',(first,)).fetchone():
                                raise HTTPException(409,'Workspace contains retained resources')
                        db.execute('BEGIN IMMEDIATE')
                        db.execute('DELETE FROM workspace_memberships WHERE workspace_id=?',(first,))
                        db.execute('DELETE FROM workspace_documents WHERE workspace_id=?',(first,))
                        db.execute('DELETE FROM workspaces WHERE id=?',(first,))
                    return None
            elif self.domain=='session':
                if name=='list':
                    return security.visible(principal,'session.read',value(*args,**kwargs))
                if name=='create':
                    workspace=kwargs.get('workspace_id')
                    if workspace:security.authorize(principal,'session.create',workspace)
                    kwargs['created_by_user_id']=principal.user_id
                else:security.session(first,principal)
            elif self.domain=='task':
                if name=='list':
                    return security.visible(principal,'task.read',value(*args,**kwargs))
                if name=='create':
                    security.authorize(principal,'task.create',first.workspace_id)
                    security.session(first.session_id,principal)
                    first.created_by_user_id=first.owner_id=principal.user_id
                    from opentelemetry import propagate
                    from server.auth.context import request_id_context
                    carrier={}; propagate.inject(carrier)
                    first.metadata.update(trace_context=carrier,request_id=request_id_context.get())
                else:
                    security.task(first,'task.read' if name in {'get','steps','events'} else 'task.execute',principal)
            elif self.domain=='memory':
                if name in {'list','retrieve_memories'}:
                    workspace=kwargs.get('workspace_id') or (args[1] if name=='retrieve_memories' and len(args)>1 else None)
                    if workspace:security.authorize(principal,'memory.read',workspace)
                    # Never honor a client/caller-supplied owner as authorization.
                    if name=='retrieve_memories':
                        return value(first,workspace,owner_id=principal.user_id,include_workspace=True)
                    kwargs.update(owner_id=principal.user_id,include_workspace=True)
                elif name=='create':
                    first=first.model_copy(deep=True); first.owner_id=principal.user_id
                    if first.scope_type=='user':first.scope_id=principal.user_id
                    else:security.authorize(principal,'memory.write',first.scope_id)
                    args=(first,*args[1:])
                else:
                    record=security.memory(first,'memory.read' if name=='get' else 'memory.write',principal)
                    if name=='get':return value(first,owner_id=record['owner_id'])
                    kwargs['owner_id']=record['owner_id']
            elif self.domain=='data':
                if name in {'artifact','register_artifact'}:
                    record=security.payload('artifacts',first) if name=='artifact' else first.model_dump()
                    security.authorize(principal,'artifact.read' if name=='artifact' else 'analysis.execute',record['workspace_id'])
                    if record.get('task_id'):security.task(record['task_id'],principal=principal)
                elif name=='artifacts':security.task(first,principal=principal)
                elif name=='save_execution':security.authorize(principal,'analysis.execute',first['workspace_id'])
                else:
                    action={'upload':'dataset.create','delete':'dataset.delete','get':'dataset.read',
                            'list':'dataset.read','inspect':'dataset.read'}.get(name)
                    if not action:raise HTTPException(403,'Operation unavailable')
                    security.authorize(principal,action,first)
            elif self.domain=='action':
                if name in {'approvals','executions'}:
                    records=value(*args,**kwargs); result=[]
                    for record in records:
                        if record.get('task_id'):
                            try:
                                if name=='approvals':security.approval(record['id'],principal=principal)
                                else:security.task(record['task_id'],principal=principal)
                                result.append(record)
                            except HTTPException:pass
                    return result
                if name in {'approval','decide'}:security.approval(first,write=name=='decide',principal=principal)
                elif name=='request':security.authorize_tool(first,args[2],principal)
                elif name=='cancel_pending':security.task(first,'task.cancel',principal)
                else:
                    record=security.payload('tool_executions',first)
                    if not record.get('task_id'):raise HTTPException(403,'Task context required')
                    security.task(record['task_id'],'task.read' if name=='execution' else 'task.execute',principal)
            elif self.domain=='agent':
                if name=='create_supervisor':
                    if first:security.task(first,'task.execute',principal)
                    metadata=dict(args[3]); metadata['actor_user_id']=principal.user_id
                    args=(*args[:3],metadata,*args[4:])
                elif name=='create_delegation':self._agent_guard(first.supervisor_run_id,principal)
                elif name in {'supervisor','runs','cancel','cached'}:
                    if first:security.task(first,'task.read' if name in {'supervisor','runs'} else 'task.execute',principal)
                elif name=='delegations':
                    task_id=kwargs.get('task_id')
                    if task_id:security.task(task_id,principal=principal)
                    elif first:self._agent_guard(first,principal)
                    else:raise HTTPException(403,'Task context required')
                elif name in {'delegation','start','finish'}:self._agent_guard(first,principal,delegation=True)
                else:self._agent_guard(first,principal)
            else:raise HTTPException(403,'Operation unavailable')
            result=value(*args,**kwargs)
            telemetry=getattr(security.manager,'telemetry',None)
            if telemetry:
                if self.domain=='task' and name=='create':telemetry.record('tasks_created_total')
                if self.domain=='action' and name=='request' and result.get('status')=='WAITING_APPROVAL':telemetry.record('approvals_requested_total')
                if self.domain=='agent' and name=='create_supervisor':telemetry.record('multi_agent_activations_total')
                if self.domain=='agent' and name=='create_delegation':telemetry.record('delegations_total')
                if self.domain=='agent' and name in {'create_supervisor','create_delegation'}:telemetry.record('agent_runs_total')
                if self.domain=='agent' and name=='reserve_llm':telemetry.record('llm_calls_total')
                if self.domain=='agent' and name=='count_call' and args[1]=='llm_calls':telemetry.record('llm_calls_total')
                if self.domain=='agent' and name=='add_usage' and isinstance(args[1],dict) and args[1].get('total_tokens') is not None:
                    telemetry.record('llm_tokens_total',args[1]['total_tokens'])
                if self.domain=='agent' and name=='create_delegation' and first.agent_id=='reviewer':telemetry.record('reviewer_runs_total')
            return result
        return call

    def _agent_guard(self,record_id,principal,delegation=False):
        record=self.security.payload('agent_delegations' if delegation else 'agent_runs',record_id)
        if record.get('task_id'):self.security.task(record['task_id'],principal=principal)
        elif delegation:self._agent_guard(record['supervisor_run_id'],principal)
        elif record.get('metadata',{}).get('actor_user_id')!=principal.user_id:
            # Delegated runs link to their actor-bearing supervisor.
            supervisor=record.get('metadata',{}).get('supervisor_run_id')
            if supervisor:self._agent_guard(supervisor,principal)
            else:raise HTTPException(403,'Access denied')


def configure_security(manager):
    store=AccountStore(manager.database or manager.store.database_path)
    manager.auth=AuthService(store,manager.config)
    manager.security=AuthorizationService(store,manager.auth,manager)
    def scope(repository,domain):return ScopedRepository(repository,manager.security,domain)
    manager.workspaces=scope(manager.workspaces,'workspace')
    manager.store=scope(manager.store,'session')
    manager.phase3.tasks=scope(manager.phase3.tasks,'task')
    manager.phase3.task_service.store=manager.phase3.tasks
    manager.phase3.memory=scope(manager.phase3.memory,'memory')
    manager.phase3.data=scope(manager.phase3.data,'data')
    manager.phase3.analysis.store=manager.phase3.data
    if manager.phase4:manager.phase4.actions=scope(manager.phase4.actions,'action')
    if manager.phase5:manager.phase5.store=scope(manager.phase5.store,'agent')
