"""universe profile columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11 23:41:58.219682
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('universe', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cik', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('sic', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('sector_etf', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('market_cap_usd', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('profile_updated_at', sa.Text(), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('universe', schema=None) as batch_op:
        batch_op.drop_column('profile_updated_at')
        batch_op.drop_column('market_cap_usd')
        batch_op.drop_column('sector_etf')
        batch_op.drop_column('sic')
        batch_op.drop_column('cik')

