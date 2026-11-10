"""raw snapshot payloads on disk

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10 20:16:13.738686
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('raw_snapshots', schema=None) as batch_op:
        batch_op.add_column(sa.Column('request_key', sa.Text(), server_default=sa.text("''"), nullable=False))
        batch_op.add_column(sa.Column('message', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('payload_path', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('payload_sha256', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('payload_bytes', sa.Integer(), nullable=True))
        batch_op.drop_index(batch_op.f('ix_raw_snapshots_source_dataset_as_of'))
        batch_op.create_index('ix_raw_snapshots_source_dataset_key_as_of', ['source', 'dataset', 'request_key', 'as_of'], unique=False)
        batch_op.drop_column('error')
        batch_op.drop_column('payload')


def downgrade() -> None:
    with op.batch_alter_table('raw_snapshots', schema=None) as batch_op:
        batch_op.add_column(sa.Column('payload', sa.TEXT(), nullable=True))
        batch_op.add_column(sa.Column('error', sa.TEXT(), nullable=True))
        batch_op.drop_index('ix_raw_snapshots_source_dataset_key_as_of')
        batch_op.create_index(batch_op.f('ix_raw_snapshots_source_dataset_as_of'), ['source', 'dataset', 'as_of'], unique=False)
        batch_op.drop_column('payload_bytes')
        batch_op.drop_column('payload_sha256')
        batch_op.drop_column('payload_path')
        batch_op.drop_column('message')
        batch_op.drop_column('request_key')

