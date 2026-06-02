"""add trim acknowledged to positions

Revision ID: e4f5a6b7c8d9
Revises: d1e2f3a4b5c6
Create Date: 2026-06-02 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("trim_acknowledged", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("positions", "trim_acknowledged")
