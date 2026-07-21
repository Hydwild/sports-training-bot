"""
Страж pre_migrate: пометить миграции применёнными («stamp») можно только
на схеме, которая действительно совпадает с моделями.

Раньше проверялось лишь наличие таблицы tenants — база без половины
колонок помечалась как мигрированная, приложение стартовало против неё,
и расхождение всплывало позже, в случайном месте.
"""
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.pre_migrate import needs_stamp
from app.models.entities import Base


def _engine():
    return create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                               connect_args={"check_same_thread": False})


async def _with_full_schema():
    engine = _engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


def test_stamp_allowed_when_schema_matches_models():
    async def run():
        engine = await _with_full_schema()
        try:
            assert await needs_stamp(engine) is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stamp_refused_when_column_missing():
    async def run():
        engine = await _with_full_schema()
        try:
            async with engine.begin() as conn:
                # имитируем схему, отставшую от моделей
                await conn.execute(text("ALTER TABLE tenants "
                                        "DROP COLUMN vertical"))
            assert await needs_stamp(engine) is False, (
                "stamp на неполной схеме: миграции будут считаться "
                "применёнными, а колонки нет")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stamp_refused_when_table_missing():
    async def run():
        engine = await _with_full_schema()
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE web_customers"))
            assert await needs_stamp(engine) is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_no_stamp_when_alembic_already_owns_db():
    async def run():
        engine = await _with_full_schema()
        try:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "CREATE TABLE alembic_version (version_num VARCHAR(32))"))
                await conn.execute(text(
                    "INSERT INTO alembic_version VALUES ('abc123')"))
            assert await needs_stamp(engine) is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ensure_columns_is_lite_only():
    """В Pro схему ведёт alembic: автодобавление колонок на старте скрывало
    бы незавершённую миграцию."""
    import inspect as _inspect

    from app import main

    src = _inspect.getsource(main._ensure_columns)
    assert "if not settings.is_sqlite:" in src
    assert "return" in src
