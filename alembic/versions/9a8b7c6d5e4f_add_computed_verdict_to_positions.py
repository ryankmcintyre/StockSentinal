"""add computed_verdict to positions

Revision ID: 9a8b7c6d5e4f
Revises: f5a6b7c8d9e0
Create Date: 2026-06-17 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9a8b7c6d5e4f"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("computed_verdict", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "computed_verdict")
