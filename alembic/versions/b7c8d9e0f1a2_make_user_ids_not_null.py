"""Make user-owned rows require user_id

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-05-19 20:21:19.261000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("DELETE FROM positions WHERE user_id IS NULL"))
    op.execute(sa.text("DELETE FROM strategy_rule_configs WHERE user_id IS NULL"))

    with op.batch_alter_table("positions") as batch_op:
        batch_op.alter_column("user_id", existing_type=sa.String(), nullable=False)

    with op.batch_alter_table("strategy_rule_configs") as batch_op:
        batch_op.alter_column("user_id", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("strategy_rule_configs") as batch_op:
        batch_op.alter_column("user_id", existing_type=sa.String(), nullable=True)

    with op.batch_alter_table("positions") as batch_op:
        batch_op.alter_column("user_id", existing_type=sa.String(), nullable=True)
