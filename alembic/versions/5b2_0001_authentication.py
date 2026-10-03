"""Add accounts, rotating sessions, workspace roles and security audit."""
from alembic import op
from sqlalchemy import Column, Text
from server.db.schema_v2 import metadata

revision = '5b2_0001'
down_revision = '5b1_0001'
branch_labels = depends_on = None


def upgrade():
    for name in ('users','refresh_tokens','workspace_memberships','audit_events'):
        metadata.tables[name].create(op.get_bind())
    for name in ('workspaces','sessions','tasks'):
        op.add_column(name, Column('created_by_user_id', Text, nullable=True))
        op.create_foreign_key(name+'_creator_fk', name, 'users', ['created_by_user_id'], ['id'])
    for name in ('sessions','tasks'):
        op.create_index(name+'_creator', name, ['created_by_user_id'])


def downgrade():
    for name in ('sessions','tasks'):
        op.drop_index(name+'_creator', table_name=name)
    for name in ('tasks','sessions','workspaces'):
        op.drop_constraint(name+'_creator_fk', name, type_='foreignkey')
        op.drop_column(name, 'created_by_user_id')
    for name in ('audit_events','workspace_memberships','refresh_tokens','users'):
        metadata.tables[name].drop(op.get_bind())
