"""Transactional account/membership/audit repository on either backend."""
import json
from server.persistence import Database, identity, now
from server.auth.context import request_id_context


class AccountStore(Database):
    def __init__(self, source):
        super().__init__(source)
        if self.backend != 'sqlite':
            return
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY,email TEXT NOT NULL,normalized_email TEXT NOT NULL UNIQUE,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN (\'ACTIVE\',\'DISABLED\')),failed_login_count INTEGER NOT NULL,locked_until TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,last_login_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS refresh_tokens (id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),token_hash TEXT NOT NULL UNIQUE,family_id TEXT NOT NULL,parent_id TEXT REFERENCES refresh_tokens(id),expires_at TEXT NOT NULL,created_at TEXT NOT NULL,used_at TEXT,revoked_at TEXT,replaced_by TEXT,user_agent_hash TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS refresh_family ON refresh_tokens(family_id)')
            db.execute('CREATE INDEX IF NOT EXISTS refresh_user ON refresh_tokens(user_id)')
            db.execute('CREATE TABLE IF NOT EXISTS workspace_memberships (workspace_id TEXT NOT NULL REFERENCES workspaces(id),user_id TEXT NOT NULL REFERENCES users(id),role TEXT NOT NULL CHECK(role IN (\'OWNER\',\'EDITOR\',\'VIEWER\')),created_at TEXT NOT NULL,created_by TEXT REFERENCES users(id),PRIMARY KEY(workspace_id,user_id))')
            db.execute('CREATE INDEX IF NOT EXISTS membership_user ON workspace_memberships(user_id)')
            db.execute('CREATE TABLE IF NOT EXISTS audit_events (id TEXT PRIMARY KEY,actor_user_id TEXT REFERENCES users(id),workspace_id TEXT,event_type TEXT NOT NULL,resource_type TEXT NOT NULL,resource_id TEXT,result TEXT NOT NULL,request_id TEXT,metadata TEXT NOT NULL,created_at TEXT NOT NULL)')
            for table in ('workspaces','sessions','tasks'):
                columns={row['name'] for row in db.execute('PRAGMA table_info('+table+')')}
                if columns and 'created_by_user_id' not in columns:
                    db.execute('ALTER TABLE '+table+' ADD COLUMN created_by_user_id TEXT REFERENCES users(id)')

    @staticmethod
    def user(db, user_id=None, email=None):
        row=db.execute('SELECT * FROM users WHERE '+('id=?' if user_id else 'normalized_email=?'),
                       (user_id or email,)).fetchone()
        return dict(row) if row else None

    def audit(self, db, kind, actor=None, workspace=None, resource_type='user', resource_id=None, result='SUCCESS', metadata=None):
        # Only bounded, predeclared operational fields can enter security audit.
        allowed={'action','role','previous_role','family_id','error_type','count','assignment','approval_id','tool_execution_id'}
        safe={k:v for k,v in (metadata or {}).items() if k in allowed and isinstance(v,(str,int,bool,type(None)))}
        db.execute('INSERT INTO audit_events (id,actor_user_id,workspace_id,event_type,resource_type,resource_id,result,request_id,metadata,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (identity(),actor,workspace,kind,resource_type,resource_id,result,request_id_context.get(),json.dumps(safe),now()))
        mapping={'LOGIN_SUCCEEDED':'auth_login_success_total','LOGIN_FAILED':'auth_login_failure_total',
                 'TOKEN_REFRESHED':'auth_refresh_total','AUTHORIZATION_DENIED':'auth_denied_total',
                 'APPROVAL_APPROVED':'approvals_approved_total','APPROVAL_REJECTED':'approvals_rejected_total'}
        if getattr(self,'telemetry',None) and kind in mapping:self.telemetry.record(mapping[kind])
        from opentelemetry import trace
        span=trace.get_current_span()
        if actor:span.set_attribute('user_id',actor)
        if workspace:span.set_attribute('workspace_id',workspace)

    def principal_user(self, user_id):
        with self.connect() as db:
            return self.user(db,user_id=user_id)
