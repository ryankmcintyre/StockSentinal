"""Alembic migration environment configuration.

Loads DATABASE_URL from .env (via python-dotenv) and uses our app models
for autogenerate support.
"""

from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from alembic import context

from app.config import get_database_url, is_postgres
from app.models import Base

# Load .env so DATABASE_URL is available outside of FastAPI startup.
load_dotenv()

# Alembic Config object — provides access to values in alembic.ini.
config = context.config

# Override the ini-file sqlalchemy.url with our runtime config.
config.set_main_option("sqlalchemy.url", get_database_url())

# Set up Python logging from the config file.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# MetaData for autogenerate support.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without connecting)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with a live connection."""
    url = config.get_main_option("sqlalchemy.url")

    # Use NullPool for PostgreSQL (Supavisor handles pooling).
    poolclass = pool.NullPool if is_postgres(url) else None

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=poolclass,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # render_as_batch=True enables batch mode for SQLite ALTER TABLE
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
