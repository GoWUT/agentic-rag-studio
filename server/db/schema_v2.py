"""Additive authentication schema; frozen v1 metadata stays unchanged."""
from sqlalchemy import MetaData, Table, Column, Text, Integer, DateTime, ForeignKey, UniqueConstraint, Index, CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB
from server.db import schema_v1

metadata = MetaData()
for old in schema_v1.metadata.tables.values():
    old.to_metadata(metadata)
LEGACY_COLUMNS = schema_v1.LEGACY_COLUMNS


def field(name, kind=Text, nullable=False, ref=None, primary=False):
    return Column(name, kind, *([ForeignKey(ref)] if ref else []), nullable=nullable, primary_key=primary)


users = Table('users', metadata, field('id', primary=True), field('email'), field('normalized_email'),
    field('display_name'), field('password_hash'), field('status'), field('failed_login_count', Integer),
    field('locked_until', DateTime(timezone=True), nullable=True), field('created_at', DateTime(timezone=True)),
    field('updated_at', DateTime(timezone=True)), field('last_login_at', DateTime(timezone=True), nullable=True),
    UniqueConstraint('normalized_email'), CheckConstraint("status IN ('ACTIVE','DISABLED')"))
refresh_tokens = Table('refresh_tokens', metadata, field('id', primary=True), field('user_id', ref='users.id'),
    field('token_hash'), field('family_id'), field('parent_id', nullable=True, ref='refresh_tokens.id'),
    field('expires_at', DateTime(timezone=True)), field('created_at', DateTime(timezone=True)),
    field('used_at', DateTime(timezone=True), nullable=True), field('revoked_at', DateTime(timezone=True), nullable=True),
    field('replaced_by', nullable=True), field('user_agent_hash', nullable=True), UniqueConstraint('token_hash'))
memberships = Table('workspace_memberships', metadata, field('workspace_id', ref='workspaces.id', primary=True),
    field('user_id', ref='users.id', primary=True), field('role'), field('created_at', DateTime(timezone=True)),
    field('created_by', nullable=True, ref='users.id'), CheckConstraint("role IN ('OWNER','EDITOR','VIEWER')"))
audit_events = Table('audit_events', metadata, field('id', primary=True), field('actor_user_id', nullable=True, ref='users.id'),
    field('workspace_id', nullable=True), field('event_type'), field('resource_type'), field('resource_id', nullable=True),
    field('result'), field('request_id', nullable=True), field('metadata', JSONB), field('created_at', DateTime(timezone=True)))
for name in ('workspaces','sessions','tasks'):
    metadata.tables[name].append_column(field('created_by_user_id', nullable=True, ref='users.id'))
Index('refresh_family', refresh_tokens.c.family_id)
Index('refresh_user', refresh_tokens.c.user_id)
Index('membership_user', memberships.c.user_id)
Index('audit_created', audit_events.c.created_at)
Index('audit_workspace', audit_events.c.workspace_id, audit_events.c.created_at)
for name in ('sessions','tasks'):
    Index(name+'_creator', metadata.tables[name].c.created_by_user_id)
for name,value in vars(schema_v1).items():
    if isinstance(value, Table):
        globals()[name] = metadata.tables[value.name]
