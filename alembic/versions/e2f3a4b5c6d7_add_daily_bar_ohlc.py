"""add daily bar ohlc

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-06-01 20:14:38.932000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("market_daily_bar_cache", sa.Column("open", sa.Float(), nullable=True))
    op.add_column("market_daily_bar_cache", sa.Column("high", sa.Float(), nullable=True))
    op.add_column("market_daily_bar_cache", sa.Column("low", sa.Float(), nullable=True))
    op.add_column("market_daily_bar_cache", sa.Column("volume", sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("market_daily_bar_cache", "volume")
    op.drop_column("market_daily_bar_cache", "low")
    op.drop_column("market_daily_bar_cache", "high")
    op.drop_column("market_daily_bar_cache", "open")
