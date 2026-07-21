"""
Проверка перед alembic upgrade head для конкретного переходного сценария:
исторически таблицы Postgres создавались автостартовым
Base.metadata.create_all (app/main.py, lifespan) ещё до того, как в проект
завели Alembic — поэтому на проде схема уже существует, а alembic_version
пуст/отсутствует. Обычный "upgrade head" в этом случае пытается заново
выполнить CREATE TABLE и падает DuplicateTableError.

needs_stamp() отличает именно этот безопасный случай от настоящих проблем
с БД. «Безопасный» — значит схема действительно совпадает с моделями:
раньше проверялось только наличие таблицы tenants, и stamp мог пометить
миграции применёнными на схеме без половины колонок. Теперь сверяются все
таблицы и все колонки моделей; при расхождении возвращается False, и
обычный upgrade либо доводит схему до конца, либо честно падает.

Стартовый скрипт делает "alembic stamp head" только когда эта функция
вернула True; любая другая ошибка миграции по-прежнему останавливает деплой.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models.entities import Base

logger = logging.getLogger("app")


def schema_gaps(sync_conn) -> list[str]:
    """Чего в базе не хватает по сравнению с моделями. Пусто — схема полна."""
    insp = inspect(sync_conn)
    existing = set(insp.get_table_names())
    gaps: list[str] = []
    for name, table in Base.metadata.tables.items():
        if name not in existing:
            gaps.append(f"нет таблицы {name}")
            continue
        have = {c["name"] for c in insp.get_columns(name)}
        for column in table.columns:
            if column.name not in have:
                gaps.append(f"нет колонки {name}.{column.name}")
    return gaps


def _check_sync(sync_conn) -> bool:
    insp = inspect(sync_conn)
    tables = insp.get_table_names()
    if "tenants" not in tables:
        return False  # схемы ещё нет вообще — обычный upgrade head отработает
    if "alembic_version" in tables:
        row = sync_conn.execute(
            text("SELECT version_num FROM alembic_version LIMIT 1")).first()
        if row is not None:
            return False        # alembic уже ведёт эту базу

    gaps = schema_gaps(sync_conn)
    if gaps:
        # схема неполная: пометить миграции применёнными нельзя — приложение
        # запустится против базы без нужных колонок
        logger.warning(
            "PRE-MIGRATE: схема неполная (%d расхождений, например: %s) — "
            "stamp не делаем, пусть отработает обычный upgrade",
            len(gaps), "; ".join(gaps[:5]))
        return False
    return True


async def needs_stamp(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        return await conn.run_sync(_check_sync)
