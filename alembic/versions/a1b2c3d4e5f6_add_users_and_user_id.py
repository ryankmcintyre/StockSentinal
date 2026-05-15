"""Add users table and user_id foreign keys

Revision ID: a1b2c3d4e5f6
Revises: 3ee6b210c351
Create Date: 2026-05-15 12:00:00.000000

SQLite note
-----------
This migration targets PostgreSQL (production).  For local SQLite development
the app uses ``Base.metadata.create_all`` + the ``_add_missing_columns`` helper
in ``app/database.py`` to manage the schema, NOT Alembic.

``_add_missing_columns`` will add the new nullable ``user_id`` column to
``positions`` and ``strategy_rule_configs`` automatically on next startup.
However, it cannot drop or replace the old unique constraint on
``strategy_rule_configs`` (``uq_strategy_rule_configs_type_key``), which
means an existing SQLite dev database will still enforce the old
``(investment_type, rule_key)`` uniqueness and will reject per-user rule
config inserts when a second user's rows conflict.

**If you have an existing SQLite dev database, delete it** (``stocksentinal.db``)
and let the app recreate it with the correct schema on next startup.  This is
safe for development; all production data is in Supabase / PostgreSQL.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '3ee6b210c351'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    op.create_table(
        'users',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=True),
        sa.Column('display_name', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    if is_pg:
        bind.execute(sa.text("ALTER TABLE users ENABLE ROW LEVEL SECURITY"))

    op.add_column('positions', sa.Column('user_id', sa.String(), nullable=True))
    op.add_column('strategy_rule_configs', sa.Column('user_id', sa.String(), nullable=True))

    if is_pg:
        op.create_foreign_key(
            'fk_positions_user_id',
            'positions', 'users',
            ['user_id'], ['id'],
            ondelete='CASCADE',
        )
        op.create_foreign_key(
            'fk_strategy_rule_configs_user_id',
            'strategy_rule_configs', 'users',
            ['user_id'], ['id'],
            ondelete='CASCADE',
        )
        op.drop_constraint('uq_strategy_rule_configs_type_key', 'strategy_rule_configs', type_='unique')
        op.create_unique_constraint(
            'uq_strategy_rule_configs_user_type_key',
            'strategy_rule_configs',
            ['user_id', 'investment_type', 'rule_key'],
        )
        op.create_index('ix_positions_user_id', 'positions', ['user_id'])
        op.create_index('ix_strategy_rule_configs_user_id', 'strategy_rule_configs', ['user_id'])


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    if is_pg:
        op.drop_index('ix_strategy_rule_configs_user_id', 'strategy_rule_configs')
        op.drop_index('ix_positions_user_id', 'positions')
        op.drop_constraint('uq_strategy_rule_configs_user_type_key', 'strategy_rule_configs', type_='unique')
        op.create_unique_constraint(
            'uq_strategy_rule_configs_type_key',
            'strategy_rule_configs',
            ['investment_type', 'rule_key'],
        )
        op.drop_constraint('fk_strategy_rule_configs_user_id', 'strategy_rule_configs', type_='foreignkey')
        op.drop_constraint('fk_positions_user_id', 'positions', type_='foreignkey')

    op.drop_column('strategy_rule_configs', 'user_id')
    op.drop_column('positions', 'user_id')
    op.drop_table('users')
