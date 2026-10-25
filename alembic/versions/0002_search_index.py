"""Persisted retrieval index.

Revision ID: 0002
Revises: 0001
"""

from alembic import op
from gw.record.tables import metadata

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.tables["search_index"].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    metadata.tables["search_index"].drop(op.get_bind(), checkfirst=True)
