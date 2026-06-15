"""add themes

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-06-15 16:39:26.929000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CURRENT_USER_ID = "NULLIF(current_setting('app.current_user_id', true), '')"


def upgrade() -> None:
    op.create_table(
        "themes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "position_themes",
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("theme_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["theme_id"], ["themes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("position_id", "theme_id"),
    )
    op.create_index("ix_themes_user_id", "themes", ["user_id"])
    op.create_index(
        "uq_themes_user_lower_name",
        "themes",
        ["user_id", sa.text("lower(name)")],
        unique=True,
    )
    op.create_index("ix_position_themes_theme_id", "position_themes", ["theme_id"])

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in ("themes", "position_themes"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")

    op.execute(
        f"""
        CREATE POLICY themes_isolation ON themes
            USING (user_id = {_CURRENT_USER_ID})
            WITH CHECK (user_id = {_CURRENT_USER_ID});
        """
    )
    op.execute(
        f"""
        CREATE POLICY position_themes_isolation ON position_themes
            USING (
                EXISTS (
                    SELECT 1 FROM positions
                    WHERE positions.id = position_themes.position_id
                      AND positions.user_id = {_CURRENT_USER_ID}
                )
                AND EXISTS (
                    SELECT 1 FROM themes
                    WHERE themes.id = position_themes.theme_id
                      AND themes.user_id = {_CURRENT_USER_ID}
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1 FROM positions
                    WHERE positions.id = position_themes.position_id
                      AND positions.user_id = {_CURRENT_USER_ID}
                )
                AND EXISTS (
                    SELECT 1 FROM themes
                    WHERE themes.id = position_themes.theme_id
                      AND themes.user_id = {_CURRENT_USER_ID}
                )
            );
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS position_themes_isolation ON position_themes;")
        op.execute("DROP POLICY IF EXISTS themes_isolation ON themes;")
        for table in ("position_themes", "themes"):
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_position_themes_theme_id", table_name="position_themes")
    op.drop_index("uq_themes_user_lower_name", table_name="themes")
    op.drop_index("ix_themes_user_id", table_name="themes")
    op.drop_table("position_themes")
    op.drop_table("themes")
