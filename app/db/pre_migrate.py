"""
Проверка перед alembic upgrade head для конкретного переходного сценария:
исторически таблицы Postgres создавались автостартовым
Base.metadata.create_all (app/main.py, lifespan) ещё до того, как в проект
завели Alembic — поэтому на проде схема уже существует, а alembic_version
пуст/отсутствует. Обычный "upgrade head" в этом случае пытается заново
выполнить CREATE TABLE и падает DuplicateTableError.

needs_stamp() отличает именно этот безопасный случай (схема уже совпадает
с тем, что дала бы миграция) от настоящих проблем с БД — стартовый скрипт
делает "alembic stamp head" вместо "upgrade head" только когда эта функция
вернула True, любая другая ошибка миграции по-прежнему останавливает деплой.
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine


def _check_sync(sync_conn) -> bool:
    insp = inspect(sync_conn)
    tables = insp.get_table_names()
    if "tenants" not in tables:
        return False  # схемы ещё нет вообще — обычный upgrade head отработает
    if "alembic_version" not in tables:
        return True
    row = sync_conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
    return row is None


async def needs_stamp(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        return await conn.run_sync(_check_sync)
