"""
Регресс: 19.07.2026 start.sh стал завершать деплой (exit 1) при неудачной
alembic-миграции (см. tasks.py история) — но на проде схема уже была
создана автостартовым create_all ДО того, как alembic_version был заведён,
поэтому обычный "upgrade head" пытался заново создать существующие таблицы
и падал DuplicateTableError на КАЖДОМ деплое, уводя Railway в crash-loop.

needs_stamp() отличает этот безопасный случай ("схема уже есть, версия не
проставлена") от реальной проблемы миграции.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.pre_migrate import needs_stamp
from app.models.entities import Base


async def _make_engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    return engine


async def test_fresh_db_does_not_need_stamp():
    engine = await _make_engine()
    try:
        assert await needs_stamp(engine) is False
    finally:
        await engine.dispose()


async def test_schema_exists_without_alembic_version_needs_stamp():
    engine = await _make_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        assert await needs_stamp(engine) is True
    finally:
        await engine.dispose()


async def test_schema_exists_with_empty_alembic_version_needs_stamp():
    engine = await _make_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        assert await needs_stamp(engine) is True
    finally:
        await engine.dispose()


async def test_schema_exists_with_stamped_version_does_not_need_stamp():
    engine = await _make_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            await conn.execute(text(
                "INSERT INTO alembic_version VALUES ('e1b635257996')"))
        assert await needs_stamp(engine) is False
    finally:
        await engine.dispose()
