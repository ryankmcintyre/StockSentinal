"""enable rls on alembic_version table

Revision ID: 3ee6b210c351
Revises: 76fe6d4b69a9
Create Date: 2026-05-06 11:19:04.526770

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3ee6b210c351'
down_revision: Union[str, Sequence[str], None] = '76fe6d4b69a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Enable RLS on the alembic_version table (PostgreSQL only).

    The previous migration (76fe6d4b69a9) covered all app tables but missed
    this Alembic-managed table.  Alembic and the app connect as the postgres
    superuser, which bypasses RLS, so this has no effect on migrations or app
    behaviour.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("ALTER TABLE alembic_version ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE alembic_version FORCE ROW LEVEL SECURITY;")


def downgrade() -> None:
    """Disable RLS on the alembic_version table (PostgreSQL only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("ALTER TABLE alembic_version NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE alembic_version DISABLE ROW LEVEL SECURITY;")
