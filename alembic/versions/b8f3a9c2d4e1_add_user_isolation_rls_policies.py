"""add user isolation rls policies

Revision ID: b8f3a9c2d4e1
Revises: a1b2c3d4e5f6
Create Date: 2026-05-19 20:32:36.685000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'b8f3a9c2d4e1'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CURRENT_USER_ID = "NULLIF(current_setting('app.current_user_id', true), '')"

_POLICIES = [
    (
        "positions",
        "positions_isolation",
        f"user_id = {_CURRENT_USER_ID}",
    ),
    (
        "strategy_rule_configs",
        "strategy_rule_configs_isolation",
        f"user_id = {_CURRENT_USER_ID}",
    ),
    (
        "position_key_levels",
        "position_key_levels_isolation",
        f"""EXISTS (
            SELECT 1
            FROM positions
            WHERE positions.id = position_key_levels.position_id
              AND positions.user_id = {_CURRENT_USER_ID}
        )""",
    ),
]


def upgrade() -> None:
    """Enforce per-user database isolation for authenticated PostgreSQL roles.

    The application sets ``app.current_user_id`` on authenticated sessions.
    These policies make RLS a real tenant-isolation boundary instead of only
    blocking anon/authenticated roles from every row.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table, policy, condition in _POLICIES:
        op.execute(
            f"""
            CREATE POLICY {policy} ON {table}
                USING ({condition})
                WITH CHECK ({condition});
            """
        )


def downgrade() -> None:
    """Remove per-user RLS policies (PostgreSQL only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table, policy, _condition in reversed(_POLICIES):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table};")
