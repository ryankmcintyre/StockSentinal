"""add user tiers and admin flags

Revision ID: c9d0e1f2a3b4
Revises: b8f3a9c2d4e1
Create Date: 2026-05-19 23:18:45.704000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b8f3a9c2d4e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('tier', sa.String(), nullable=False, server_default='free'),
    )
    op.add_column(
        'users',
        sa.Column('is_admin', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'users',
        sa.Column('refresh_count_today', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column('users', sa.Column('refresh_count_date', sa.Date(), nullable=True))

    op.execute("UPDATE users SET tier = 'full_access'")


def downgrade() -> None:
    op.drop_column('users', 'refresh_count_date')
    op.drop_column('users', 'refresh_count_today')
    op.drop_column('users', 'is_admin')
    op.drop_column('users', 'tier')
