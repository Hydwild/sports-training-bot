"""Alembic env: async-движок, target_metadata из моделей, URL из настроек."""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from app.core.config import settings
from app.db.migration_settings import lock_timeout
from app.models.entities import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

# URL берём из настроек приложения (env), а не из alembic.ini
config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = Base.metadata


def do_run_migrations(connection) -> None:
    if connection.dialect.name == "postgresql":
        # SQLite блокировок уровня строк/таблиц в этом смысле не имеет —
        # там параметр не поддерживается и не нужен.
        connection.exec_driver_sql(f"SET lock_timeout = '{lock_timeout()}'")
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    context.configure(url=settings.database_url, target_metadata=target_metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
