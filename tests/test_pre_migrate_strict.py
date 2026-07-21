"""
Страж pre_migrate: `alembic stamp head` на legacy-базе разрешён только при
ДВУХ условиях сразу — оператор явно разрешил (ALLOW_LEGACY_STAMP) и схема
ГЛУБОКО совпадает с моделями (таблицы, колонки, типы, nullable, PK, FK,
unique, индексы).

Раньше проверялось лишь наличие таблицы tenants: база без половины колонок
или с неверными ограничениями помечалась как мигрированная, приложение
стартовало против неё, и расхождение всплывало позже, в случайном месте.
"""
import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.pre_migrate import ALLOW_FLAG, needs_stamp
from app.models.entities import Base


@pytest.fixture(autouse=True)
def _allow_stamp(monkeypatch):
    """По умолчанию в тестах флаг задан — иначе _check_sync отказывает
    ещё до сверки схемы. Тест без флага задаёт это явно."""
    monkeypatch.setenv(ALLOW_FLAG, "1")


def _engine():
    return create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                               connect_args={"check_same_thread": False})


async def _with_full_schema():
    engine = _engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


def _run(coro):
    asyncio.run(coro)


# ---------- флаг оператора ----------

def test_no_stamp_without_operator_flag(monkeypatch):
    monkeypatch.delenv(ALLOW_FLAG, raising=False)

    async def run():
        engine = await _with_full_schema()
        try:
            # схема идеальна, но флага нет — обычный деплой не должен
            # штамповать молча
            assert await needs_stamp(engine) is False
        finally:
            await engine.dispose()

    _run(run())


def test_stamp_allowed_with_flag_and_matching_schema():
    async def run():
        engine = await _with_full_schema()
        try:
            assert await needs_stamp(engine) is True
        finally:
            await engine.dispose()

    _run(run())


# ---------- глубокая сверка схемы ----------

def test_stamp_refused_when_column_missing():
    async def run():
        engine = await _with_full_schema()
        try:
            async with engine.begin() as conn:
                await conn.execute(text("ALTER TABLE tenants "
                                        "DROP COLUMN vertical"))
            assert await needs_stamp(engine) is False
        finally:
            await engine.dispose()

    _run(run())


def test_stamp_refused_when_table_missing():
    async def run():
        engine = await _with_full_schema()
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE web_customers"))
            assert await needs_stamp(engine) is False
        finally:
            await engine.dispose()

    _run(run())


def test_stamp_refused_when_type_wrong():
    """Колонка есть, но тип другой — приложение сломается на первом же
    запросе, а stamp это скрыл бы."""
    async def run():
        engine = _engine()
        # tenants.is_demo в модели BOOLEAN; создаём вручную с колонкой-строкой
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # SQLite не умеет ALTER TYPE — пересобираем одну таблицу грубо
            await conn.execute(text("DROP TABLE signups"))
            await conn.execute(text(
                "CREATE TABLE signups (id INTEGER PRIMARY KEY, "
                "tenant_id TEXT)"))   # tenant_id должен быть INTEGER+FK
        try:
            from app.db.pre_migrate import schema_diffs
            async with engine.connect() as conn:
                diffs = await conn.run_sync(schema_diffs)
            assert any("signups" in d for d in diffs), diffs
            assert await needs_stamp(engine) is False
        finally:
            await engine.dispose()

    _run(run())


def test_stamp_refused_when_unique_constraint_missing():
    async def run():
        engine = _engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # web_customers имеет unique (tenant_id, phone_index) — пересоздаём
            # без него
            await conn.execute(text("DROP TABLE web_customers"))
            await conn.execute(text(
                "CREATE TABLE web_customers ("
                "id INTEGER PRIMARY KEY, tenant_id INTEGER, "
                "phone_index VARCHAR(64), phone_enc TEXT, "
                "key_ver VARCHAR(8), index_ver VARCHAR(8), "
                "name VARCHAR(200), created_at DATETIME)"))
        try:
            from app.db.pre_migrate import schema_diffs
            async with engine.connect() as conn:
                diffs = await conn.run_sync(schema_diffs)
            assert any("уникаль" in d and "web_customers" in d
                       for d in diffs), diffs
            assert await needs_stamp(engine) is False
        finally:
            await engine.dispose()

    _run(run())


def test_stamp_refused_when_index_missing():
    async def run():
        engine = _engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # снимаем индекс rate_buckets.bucket_key
            idxs = await conn.run_sync(
                lambda c: [i["name"] for i in
                           __import__("sqlalchemy").inspect(c)
                           .get_indexes("rate_buckets")])
            for name in idxs:
                if "bucket_key" in name:
                    await conn.execute(text(f"DROP INDEX {name}"))
        try:
            from app.db.pre_migrate import schema_diffs
            async with engine.connect() as conn:
                diffs = await conn.run_sync(schema_diffs)
            assert any("индекс" in d and "rate_buckets" in d
                       for d in diffs), diffs
            assert await needs_stamp(engine) is False
        finally:
            await engine.dispose()

    _run(run())


def test_full_schema_has_no_diffs():
    """create_all даёт схему без единого расхождения — иначе строгая
    проверка отвергала бы даже правильную legacy-базу."""
    async def run():
        engine = await _with_full_schema()
        try:
            from app.db.pre_migrate import schema_diffs
            async with engine.connect() as conn:
                diffs = await conn.run_sync(schema_diffs)
            assert diffs == [], diffs
        finally:
            await engine.dispose()

    _run(run())


# ---------- прочее ----------

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

    _run(run())


def test_ensure_columns_is_lite_only():
    """В Pro схему ведёт alembic: автодобавление колонок на старте скрывало
    бы незавершённую миграцию."""
    import inspect as _inspect

    from app import main

    src = _inspect.getsource(main._ensure_columns)
    assert "if not settings.is_sqlite:" in src
    assert "return" in src
