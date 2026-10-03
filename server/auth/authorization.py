"""One live workspace policy shared by API, services and worker execution."""
import json
from contextlib import contextmanager
from fastapi import HTTPException
from server.persistence import identity, now
from server.auth.context import require_principal, principal_scope


READ_ACTIONS={'workspace.read','document.read','dataset.read','artifact.read','memory.read',
              'external.read','session.create','session.read','task.read','task.create','task.execute','task.cancel'}
EDITOR_ACTIONS=READ_ACTIONS|{'document.create','document.delete','dataset.create','dataset.delete',
                            'memory.write','analysis.execute'}
OWNER_ACTIONS=EDITOR_ACTIONS|{'workspace.update','workspace.delete','workspace.members.manage','external.write'}
ROLE_MATRIX={'OWNER':OWNER_ACTIONS,'EDITOR':EDITOR_ACTIONS,'VIEWER':READ_ACTIONS}


class AuthorizationService:
    def __init__(self,store,auth,manager):
        self.store,self.auth,self.manager=store,auth,manager

    @contextmanager
    def as_principal(self,principal):
        self.auth.principal_for_user(principal.user_id)
        with principal_scope(principal):yield principal

    def role(self,principal,workspace_id,db=None):
        principal=require_principal(principal)
        def find(connection):
            row=connection.execute("SELECT m.role FROM workspace_memberships m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? AND m.user_id=? AND u.status='ACTIVE'",(workspace_id,principal.user_id)).fetchone()
            return row[0] if row else None
        if db is not None:return find(db)
        with self.store.connect() as connection:return find(connection)

    def authorize(self,principal,action,workspace_id,creator_id=None,*,audit=True):
        principal=require_principal(principal)
        role=self.role(principal,workspace_id)
        allowed=self.allowed(principal,role,action,creator_id)
        if allowed:return role
        if audit:
            with self.store.connect() as db:
                self.store.audit(db,'AUTHORIZATION_DENIED',actor=principal.user_id,workspace=workspace_id,
                                 resource_type='workspace',result='DENIED',metadata={'action':action})
        raise HTTPException(403,'Access denied')

    @staticmethod
    def allowed(principal,role,action,creator_id=None):
        allowed=role in ROLE_MATRIX and action in ROLE_MATRIX[role]
        if action=='session.read':allowed=allowed and creator_id==principal.user_id
        if action=='task.read' and role=='VIEWER':allowed=allowed and creator_id==principal.user_id
        if action in {'task.execute','task.cancel'} and role!='OWNER':allowed=allowed and creator_id==principal.user_id
        return allowed

    def visible(self,principal,action,records):
        """One fresh membership query for a read-only list, not one per row.

        This snapshot never authorizes a later mutation/tool call. Those continue
        to check live membership independently at their execution boundaries.
        """
        principal=require_principal(principal)
        self.auth.principal_for_user(principal.user_id)
        with self.store.connect() as db:
            roles=dict((r['workspace_id'],r['role']) for r in db.execute(
                "SELECT m.workspace_id,m.role FROM workspace_memberships m JOIN users u ON u.id=m.user_id WHERE m.user_id=? AND u.status='ACTIVE'",(principal.user_id,)))
        result=[]
        for record in records:
            workspace=record.id if action=='workspace.read' else record.workspace_id
            creator=getattr(record,'created_by_user_id',None)
            if action=='session.read' and not workspace:
                allowed=creator==principal.user_id
            else:allowed=self.allowed(principal,roles.get(workspace),action,creator)
            if allowed:result.append(record)
        return result

    def can(self,principal,action,workspace_id,creator_id=None):
        try:self.authorize(principal,action,workspace_id,creator_id,audit=False); return True
        except HTTPException:return False

    def task(self,task_id,action='task.read',principal=None):
        with self.store.connect() as db:
            row=db.execute('SELECT payload FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not row:raise HTTPException(404,'Resource not found')
        value=json.loads(row[0]); creator=value.get('created_by_user_id')
        self.authorize(principal,action,value['workspace_id'],creator)
        return value

    def session(self,session_id,principal=None):
        with self.store.connect() as db:
            row=db.execute('SELECT workspace_id,created_by_user_id FROM sessions WHERE session_id=?',(session_id,)).fetchone()
        if not row:raise HTTPException(404,'Resource not found')
        if row['workspace_id']:
            self.authorize(principal,'session.read',row['workspace_id'],row['created_by_user_id'])
        elif row['created_by_user_id']!=require_principal(principal).user_id:
            raise HTTPException(403,'Access denied')
        return dict(row)

    def payload(self,table,record_id):
        if table not in {'long_term_memories','artifacts','approval_requests','tool_executions','agent_runs','agent_delegations'}:
            raise ValueError('Unsupported authorization resource')
        with self.store.connect() as db:
            row=db.execute('SELECT payload FROM '+table+' WHERE id=?',(record_id,)).fetchone()
        if not row:raise HTTPException(404,'Resource not found')
        return json.loads(row[0])

    def memory(self,memory_id,action='memory.read',principal=None):
        value=self.payload('long_term_memories',memory_id); principal=require_principal(principal)
        if value['scope_type']=='user':
            if value['owner_id']!=principal.user_id or value['scope_id']!=principal.user_id:
                raise HTTPException(403,'Access denied')
            self.auth.principal_for_user(principal.user_id)
        else:self.authorize(principal,action,value['scope_id'])
        return value

    def approval(self,approval_id,*,write=False,principal=None):
        value=self.payload('approval_requests',approval_id)
        task=self.task(value['task_id'],principal=principal)
        descriptor=value['descriptor']
        if write or descriptor.get('operation_type')!='read':
            self.authorize(principal,'external.write',task['workspace_id'])
        elif task.get('created_by_user_id')!=require_principal(principal).user_id:
            self.authorize(principal,'workspace.members.manage',task['workspace_id'])
        return value

    def authorize_tool(self,descriptor,state,principal=None):
        if state.get('task_id'):
            task=self.task(state['task_id'],'task.execute',principal)
            workspace=task['workspace_id']
            if task.get('created_by_user_id')!=require_principal(principal).user_id:
                raise HTTPException(403,'Execution actor mismatch')
        else:
            self.session(state.get('session_id',''),principal)
            workspace=state.get('workspace_id')
        if not workspace:raise HTTPException(403,'Workspace context required')
        action='external.read'
        if descriptor.provider_type=='mcp' and descriptor.operation_type!='read':action='external.write'
        elif descriptor.capability in {'data.analyze','analysis.execute'}:action='analysis.execute'
        elif descriptor.capability.startswith('memory.') and descriptor.operation_type!='read':action='memory.write'
        self.authorize(principal,action,workspace)

    def create_workspace(self,name,description='',principal=None):
        principal=require_principal(principal); self.auth.principal_for_user(principal.user_id)
        from server.workspaces import Workspace
        stamp=now(); value=Workspace(id=identity(),name=name.strip(),description=description,created_at=stamp,updated_at=stamp)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO workspaces (id,name,description,created_at,updated_at,created_by_user_id) VALUES (?,?,?,?,?,?)',
                       (value.id,value.name,value.description,stamp,stamp,principal.user_id))
            db.execute('INSERT INTO workspace_memberships (workspace_id,user_id,role,created_at,created_by) VALUES (?,?,?,?,?)',
                       (value.id,principal.user_id,'OWNER',stamp,principal.user_id))
            self.store.audit(db,'WORKSPACE_CREATED',actor=principal.user_id,workspace=value.id,resource_type='workspace',resource_id=value.id)
        return value

    def members(self,workspace_id,principal=None):
        self.authorize(principal,'workspace.members.manage',workspace_id)
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT m.user_id,m.role,u.email,u.display_name,m.created_at FROM workspace_memberships m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?',(workspace_id,))]

    def change_member(self,workspace_id,*,email=None,user_id=None,role=None,remove=False,add=False,principal=None):
        principal=require_principal(principal)
        if not remove and role not in ROLE_MATRIX:raise HTTPException(422,'Invalid role')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.role(principal,workspace_id,db)!='OWNER':raise HTTPException(403,'Access denied')
            user=self.store.user(db,user_id=user_id,email=email.strip().lower() if email else None)
            if not user:raise HTTPException(404,'User not found')
            member=db.execute('SELECT role FROM workspace_memberships WHERE workspace_id=? AND user_id=?',(workspace_id,user['id'])).fetchone()
            if add and member:raise HTTPException(409,'Member already exists')
            if not add and not member:raise HTTPException(404,'Member not found')
            if member and member['role']=='OWNER' and (remove or role!='OWNER'):
                owners=db.execute("SELECT count(*) FROM workspace_memberships WHERE workspace_id=? AND role='OWNER'",(workspace_id,)).fetchone()[0]
                if owners<=1:raise HTTPException(409,'Workspace must retain an OWNER')
            if remove:
                db.execute('DELETE FROM workspace_memberships WHERE workspace_id=? AND user_id=?',(workspace_id,user['id']))
                event='MEMBER_REMOVED'
            elif add:
                db.execute('INSERT INTO workspace_memberships (workspace_id,user_id,role,created_at,created_by) VALUES (?,?,?,?,?)',
                           (workspace_id,user['id'],role,now(),principal.user_id)); event='MEMBER_ADDED'
            else:
                db.execute('UPDATE workspace_memberships SET role=? WHERE workspace_id=? AND user_id=?',(role,workspace_id,user['id'])); event='MEMBER_ROLE_CHANGED'
            self.store.audit(db,event,actor=principal.user_id,workspace=workspace_id,resource_type='membership',
                resource_id=user['id'],metadata={'role':role,'previous_role':member['role'] if member else None})
        return {'user_id':user['id'],'role':None if remove else role}
