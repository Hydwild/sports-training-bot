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
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    kwargs = {}
    if "asyncpg" in settings.database_url:
        # lock_timeout задаём ПАРАМЕТРОМ СОЕДИНЕНИЯ, а не отдельным SET
        # внутри транзакции миграции: лишний запрос в той же транзакции
        # ломал `alembic check` (проверку схемы на PostgreSQL).
        #
        # Ограничивается только ожидание блокировки: ALTER TABLE требует
        # ACCESS EXCLUSIVE, а во время деплоя старый контейнер ещё держит
        # запросы к тем же таблицам, и по умолчанию PostgreSQL ждёт
        # бесконечно — start.sh не доходит до uvicorn, и деплой висит без
        # единой строки в логах. Длительность самой миграции не ограничена,
        # долгий бэкфилл не прервётся.
        kwargs["connect_args"] = {
            "server_settings": {"lock_timeout": lock_timeout()}
        }
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        **kwargs,
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
