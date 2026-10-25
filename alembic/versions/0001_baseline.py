"""Baseline: every table of the rev B record.

Revision ID: 0001
Revises:
"""

from alembic import op
from gw.record.tables import metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(op.get_bind())
