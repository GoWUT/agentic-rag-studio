"""Operator CLI. No default account/password; legacy ownership requires a named user."""
import argparse
import getpass
import json
from fastapi import HTTPException
from server.config import CONFIG
from server.sessions import AgentSessionManager
from server.persistence import now


def assign_legacy(manager,email,dry_run=True):
    store=manager.auth.store
    with store.connect() as db:
        if not dry_run:db.execute('BEGIN IMMEDIATE')
        user=store.user(db,email=email.strip().lower())
        if not user:raise ValueError('Target user does not exist')
        workspaces=[dict(r) for r in db.execute('SELECT w.id FROM workspaces w WHERE w.created_by_user_id IS NULL AND NOT EXISTS (SELECT 1 FROM workspace_memberships m WHERE m.workspace_id=w.id)')]
        ids={w['id'] for w in workspaces}
        sessions=[dict(r) for r in db.execute('SELECT session_id,workspace_id FROM sessions WHERE created_by_user_id IS NULL')
                  if r['workspace_id'] in ids or not r['workspace_id']]
        tasks=[json.loads(r[0]) for r in db.execute('SELECT payload FROM tasks WHERE created_by_user_id IS NULL')]
        tasks=[t for t in tasks if t.get('owner_id','local_default')=='local_default' and t['workspace_id'] in ids]
        if any(t['status'] in {'RUNNING','QUEUED','PAUSED','WAITING_USER','REQUIRES_RECONCILIATION'} for t in tasks):
            raise ValueError('Complete or cancel active legacy tasks before ownership assignment')
        memories=[json.loads(r[0]) for r in db.execute('SELECT payload FROM long_term_memories WHERE owner_id=?',('local_default',))]
        memories=[m for m in memories if m['scope_type']=='user' or m['scope_id'] in ids]
        report={'mode':'dry-run' if dry_run else 'assigned','target_user_id':user['id'],
                'workspaces':len(ids),'sessions':len(sessions),'tasks':len(tasks),'memories':len(memories),
                'owner_memberships':len(ids)}
        if not dry_run:
            for workspace in workspaces:
                db.execute('UPDATE workspaces SET created_by_user_id=? WHERE id=?',(user['id'],workspace['id']))
                db.execute('INSERT INTO workspace_memberships (workspace_id,user_id,role,created_at,created_by) VALUES (?,?,?,?,?)',
                           (workspace['id'],user['id'],'OWNER',now(),user['id']))
            for session in sessions:
                db.execute('UPDATE sessions SET created_by_user_id=? WHERE session_id=?',(user['id'],session['session_id']))
            for task in tasks:
                task.update(owner_id=user['id'],created_by_user_id=user['id'],version=task.get('version',0)+1)
                if store.backend=='postgresql':
                    db.execute('UPDATE tasks SET payload=?,created_by_user_id=?,version=? WHERE id=?',
                               (json.dumps(task),user['id'],task['version'],task['id']))
                else:db.execute('UPDATE tasks SET payload=?,created_by_user_id=? WHERE id=?',(json.dumps(task),user['id'],task['id']))
            for memory in memories:
                memory['owner_id']=user['id']
                if memory['scope_type']=='user':memory['scope_id']=user['id']
                db.execute('UPDATE long_term_memories SET owner_id=?,scope_id=?,payload=? WHERE id=?',
                           (user['id'],memory['scope_id'],json.dumps(memory),memory['id']))
            store.audit(db,'LEGACY_OWNER_ASSIGNED',actor=user['id'],resource_type='migration',metadata={'count':sum(report[k] for k in ('workspaces','sessions','tasks','memories'))})
        return report


def main():
    parser=argparse.ArgumentParser()
    commands=parser.add_subparsers(dest='command',required=True)
    create=commands.add_parser('create-user'); create.add_argument('--email',required=True); create.add_argument('--display-name',default='')
    reset=commands.add_parser('reset-user-password'); reset.add_argument('--email',required=True)
    status=commands.add_parser('set-user-status'); status.add_argument('--email',required=True); status.add_argument('--status',choices=['ACTIVE','DISABLED'],required=True)
    assignment=commands.add_parser('assign-legacy-data'); assignment.add_argument('--user',required=True)
    mode=assignment.add_mutually_exclusive_group(required=True); mode.add_argument('--dry-run',action='store_true'); mode.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if not CONFIG.get('AUTH_ENABLED'):raise ValueError('Operator account management requires AUTH_ENABLED=true')
    manager=AgentSessionManager(CONFIG)
    try:
        if args.command=='create-user':
            password=getpass.getpass('Password: ')
            if password!=getpass.getpass('Confirm password: '):raise ValueError('Passwords differ')
            result=manager.auth.register(args.email,password,args.display_name)
            print(json.dumps({'user_id':result['id'],'status':result['status']}))
        elif args.command=='assign-legacy-data':print(json.dumps(assign_legacy(manager,args.user,args.dry_run),indent=2))
        else:
            store=manager.auth.store
            with store.connect() as db:user=store.user(db,email=args.email.strip().lower())
            if not user:raise ValueError('User does not exist')
            if args.command=='reset-user-password':
                password=getpass.getpass('New password: ')
                if password!=getpass.getpass('Confirm password: '):raise ValueError('Passwords differ')
                hashed=manager.auth.passwords.hash_password(password)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if args.command=='reset-user-password':db.execute('UPDATE users SET password_hash=?,failed_login_count=0,locked_until=NULL,updated_at=? WHERE id=?',(hashed,now(),user['id']))
                else:db.execute('UPDATE users SET status=?,updated_at=? WHERE id=?',(args.status,now(),user['id']))
                db.execute('UPDATE refresh_tokens SET revoked_at=? WHERE user_id=?',(now(),user['id']))
                store.audit(db,'PASSWORD_CHANGED' if args.command=='reset-user-password' else 'USER_STATUS_CHANGED',
                            actor=user['id'],metadata={'action':'operator'})
            print('Account updated; refresh sessions revoked')
    finally:
        if manager.phase4:manager.phase4.close()
        if manager.database:manager.database.sync_engine.dispose()


if __name__=='__main__':
    try:main()
    except (ValueError,HTTPException) as error:
        raise SystemExit(getattr(error,'detail',str(error))) from None
