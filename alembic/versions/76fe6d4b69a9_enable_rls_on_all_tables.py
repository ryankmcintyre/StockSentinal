"""enable rls on all tables

Revision ID: 76fe6d4b69a9
Revises: ff2d81d934e3
Create Date: 2026-05-06 10:33:49.931674

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '76fe6d4b69a9'
down_revision: Union[str, Sequence[str], None] = 'ff2d81d934e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = [
    "alembic_version",
    "market_atr_cache",
    "market_daily_bar_cache",
    "market_indicator_cache",
    "market_weekly_bar_cache",
    "position_key_levels",
    "positions",
    "strategy_rule_configs",
]


def upgrade() -> None:
    """Enable Row-Level Security on all tables (PostgreSQL only).

    With RLS enabled and no permissive policies, the Supabase public REST API
    (PostgREST / anon role) is blocked from reading or writing any data.
    The application's direct database connection uses the postgres superuser,
    which bypasses RLS entirely, so app behaviour is unaffected.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")


def downgrade() -> None:
    """Disable Row-Level Security on all tables (PostgreSQL only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
