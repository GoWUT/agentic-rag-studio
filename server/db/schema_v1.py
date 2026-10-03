"""Versioned application metadata. Queue/checkpoint SDK schemas are separate."""
from sqlalchemy import (MetaData, Table, Column, Text, Integer, BigInteger,
                        DateTime, ForeignKey, UniqueConstraint, Index, Identity, func)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
LEGACY_COLUMNS = {}


def table(name, *columns, constraints=(), indexes=()):
    LEGACY_COLUMNS[name] = [c.name for c in columns]
    value = Table(name, metadata, *columns, *constraints,
                  Column('_order', BigInteger, Identity(), nullable=False, unique=True))
    for suffix, fields in indexes:
        Index(name + '_' + suffix, *(value.c[f] for f in fields))
    return value


def col(name, kind=Text, *, nullable=False, ref=None, primary=False, unique=False):
    return Column(name, kind, *([ForeignKey(ref)] if ref else []),
                  nullable=nullable, primary_key=primary, unique=unique)


workspaces = table('workspaces', col('id', primary=True), col('name'), col('description'),
                   col('created_at', DateTime(timezone=True)), col('updated_at', DateTime(timezone=True)))
sessions = table('sessions', col('session_id', primary=True), col('file_id'), col('file_name'),
                 col('pdf_path'), col('chroma_dir'), col('embedding_model'), col('messages_json', JSONB),
                 col('created_at', DateTime(timezone=True)), col('updated_at', DateTime(timezone=True)),
                 col('workspace_id', nullable=True, ref='workspaces.id'),
                 indexes=(('workspace', ('workspace_id',)),))
documents = table('workspace_documents', col('id', primary=True), col('workspace_id', ref='workspaces.id'),
                  col('filename'), col('display_name'), col('fingerprint'), col('index_id'), col('status'),
                  col('page_count', Integer, nullable=True), col('created_at', DateTime(timezone=True)), col('metadata', JSONB),
                  constraints=(UniqueConstraint('workspace_id', 'fingerprint', 'index_id'),),
                  indexes=(('workspace', ('workspace_id',)),))
tasks = table('tasks', col('id', primary=True), col('workspace_id', ref='workspaces.id'),
              col('session_id', ref='sessions.session_id'), col('status'), col('payload', JSONB),
              indexes=(('status', ('status',)), ('workspace', ('workspace_id',))))
steps = table('task_steps', col('id', primary=True), col('task_id', ref='tasks.id'),
              col('step_index', Integer), col('status'), col('payload', JSONB),
              constraints=(UniqueConstraint('task_id', 'step_index'),))
events = table('task_events', col('id', primary=True), col('task_id', ref='tasks.id'), col('event_type'), col('payload', JSONB),
               indexes=(('task', ('task_id',)),))
memories = table('long_term_memories', col('id', primary=True), col('owner_id'), col('scope_type'), col('scope_id'),
                 col('memory_type'), col('status'), col('content_hash'), col('payload', JSONB), col('embedding', JSONB),
                 indexes=(('scope', ('owner_id','scope_type','scope_id','status')), ('hash', ('owner_id','scope_id','content_hash'))))
assets = table('data_assets', col('id', primary=True), col('workspace_id', ref='workspaces.id'), col('fingerprint'), col('payload', JSONB),
               constraints=(UniqueConstraint('workspace_id','fingerprint'),), indexes=(('workspace', ('workspace_id',)),))
analysis = table('analysis_executions', col('id', primary=True), col('workspace_id', ref='workspaces.id'),
                 col('task_id', nullable=True, ref='tasks.id'), col('payload', JSONB))
artifacts = table('artifacts', col('id', primary=True), col('workspace_id', ref='workspaces.id'),
                  col('task_id', nullable=True, ref='tasks.id'), col('execution_id', ref='analysis_executions.id'), col('payload', JSONB),
                  indexes=(('task', ('task_id',)),))
executions = table('tool_executions', col('id', primary=True), col('intent_key', unique=True),
                   col('task_id', nullable=True, ref='tasks.id'), col('status'), col('payload', JSONB), indexes=(('task', ('task_id',)),))
approvals = table('approval_requests', col('id', primary=True), col('tool_execution_id', unique=True, ref='tool_executions.id'),
                  col('task_id', ref='tasks.id'), col('status'), col('payload', JSONB), indexes=(('task_status', ('task_id','status')),))
runs = table('agent_runs', col('id', primary=True), col('task_id', nullable=True, ref='tasks.id'),
             col('delegation_id', nullable=True, unique=True), col('agent_id'), col('status'), col('payload', JSONB),
             indexes=(('task', ('task_id',)), ('status', ('status',))))
delegations = table('agent_delegations', col('id', primary=True), col('task_id', nullable=True, ref='tasks.id'),
                    col('supervisor_run_id', ref='agent_runs.id'), col('agent_id'), col('status'), col('payload', JSONB),
                    indexes=(('task', ('task_id',)), ('status', ('status',))))
mcp = table('mcp_servers', col('id', primary=True), col('payload', JSONB))
indexes = table('indexes', col('file_id', primary=True), col('file_name'), col('size_bytes', BigInteger), col('status'),
                col('error', nullable=True), col('owner_pid', Integer, nullable=True),
                col('created_at', DateTime(timezone=True)), col('updated_at', DateTime(timezone=True)))

# Indexed projection fields; domain payloads remain the public source of truth.
for name in ('tasks','task_steps','task_events','long_term_memories','data_assets',
             'analysis_executions','artifacts','tool_executions','approval_requests','agent_runs','agent_delegations','mcp_servers'):
    metadata.tables[name].append_column(Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()))
Index('task_events_task_created', events.c.task_id, events.c.created_at)
for column in (Column('version', Integer, nullable=False, server_default='0'),
               Column('queue_job_id', BigInteger), Column('queued_at', DateTime(timezone=True)),
               Column('worker_started_at', DateTime(timezone=True)),
               Column('attempt_count', Integer, nullable=False, server_default='0'),
               Column('execution_backend', Text, nullable=False, server_default='inline')):
    tasks.append_column(column)
