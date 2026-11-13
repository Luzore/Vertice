"""plan details and position tracking

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-13 00:30:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('ideas', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reason', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('details', sa.Text(), nullable=True))
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tracking', sa.Text(), nullable=True))
    with op.batch_alter_table('news', schema=None) as batch_op:
        batch_op.add_column(sa.Column('classified_by', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('news', schema=None) as batch_op:
        batch_op.drop_column('classified_by')
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.drop_column('tracking')
    with op.batch_alter_table('ideas', schema=None) as batch_op:
        batch_op.drop_column('details')
        batch_op.drop_column('reason')
