"""Initial Phase 1–5A.5 application schema and worker task projections.

Revision ID: 5b1_0001
"""
from alembic import op
from server.db.schema_v1 import metadata

revision = '5b1_0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade():
    # Explicit operator migration only, never invoked by the application.
    metadata.drop_all(op.get_bind(), checkfirst=False)
