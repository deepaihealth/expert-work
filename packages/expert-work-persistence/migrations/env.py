"""Alembic env — supports both online (real DB) and offline (--sql) modes.

URL resolution order:
1. ``EXPERT_WORK_DB_URL`` env var (preferred — set by deploy scripts / tests)
2. ``sqlalchemy.url`` from alembic.ini (dev default)
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from expert_work.persistence.base import Base
from expert_work.persistence.models import AuditLogRow, EventLogRow, ThreadMetaRow  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if (env_url := os.environ.get("EXPERT_WORK_DB_URL")) is not None:
    # set_main_option values pass through configparser interpolation, so a
    # literal ``%`` (e.g. a URL-encoded ``%40`` for ``@`` in the password)
    # must be escaped as ``%%`` or alembic dies with "invalid interpolation
    # syntax".
    config.set_main_option("sqlalchemy.url", env_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emits SQL only, no DB connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the real DB."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
