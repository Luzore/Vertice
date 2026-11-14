"""alert details and notification tracking

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14 00:30:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('data', sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column('notified_count', sa.Integer(), server_default=sa.text('0'), nullable=False)
        )
        batch_op.add_column(sa.Column('last_notified_at', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.drop_column('last_notified_at')
        batch_op.drop_column('notified_count')
        batch_op.drop_column('data')
