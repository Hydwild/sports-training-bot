"""
Общий счётчик частоты запросов.

Счётчик в памяти процесса лимитом является, только пока процесс один: два
воркера дают двойной лимит, а перезапуск обнуляет счёт. В Pro счётчик
общий — строка в таблице с атомарным инкрементом.
"""
import asyncio
import time

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import rate_limit
from app.models.entities import Base, RateBucket


@pytest.fixture(autouse=True)
def _clean():
    rate_limit._memory.clear()
    yield
    rate_limit._memory.clear()


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


# ---------- ключ ----------

def test_key_separates_form_club_and_client():
    a = rate_limit.bucket_key("signup", 1, "1.2.3.4")
    assert a != rate_limit.bucket_key("signup", 2, "1.2.3.4")   # другой клуб
    assert a != rate_limit.bucket_key("my", 1, "1.2.3.4")       # другая форма
    assert a != rate_limit.bucket_key("signup", 1, "5.6.7.8")   # другой клиент
    # ключ не растёт бесконтрольно от длинного «адреса»
    assert len(rate_limit.bucket_key("signup", 1, "x" * 500)) <= 200


# ---------- счётчик в памяти (Lite/dev) ----------

def test_memory_counter_blocks_and_reports_retry():
    key = rate_limit.bucket_key("signup", 1, "1.1.1.1")
    for _ in range(5):
        ok, retry = rate_limit.check_memory(key, 5, 60)
        assert ok and retry == 0
    ok, retry = rate_limit.check_memory(key, 5, 60)
    assert not ok
    assert 0 < retry <= 61


def test_memory_counter_is_per_key():
    a = rate_limit.bucket_key("signup", 1, "1.1.1.1")
    b = rate_limit.bucket_key("signup", 2, "1.1.1.1")
    for _ in range(6):
        rate_limit.check_memory(a, 5, 60)
    assert rate_limit.check_memory(a, 5, 60)[0] is False
    assert rate_limit.check_memory(b, 5, 60)[0] is True


# ---------- общее хранилище ----------

def test_shared_storage_only_with_postgres(monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "database_url",
                        "sqlite+aiosqlite:///./x.db")
    assert rate_limit.shared_storage() is False
    monkeypatch.setattr(rate_limit.settings, "database_url",
                        "postgresql+asyncpg://u:p@h/db")
    assert rate_limit.shared_storage() is True


async def test_bucket_rows_are_windowed_and_purged(maker):
    """Старые окна не должны копиться вечно."""
    async with maker() as s:
        old = int(time.time()) - 7200
        s.add(RateBucket(bucket_key="signup|1|1.2.3.4", window_start=old,
                         hits=5))
        s.add(RateBucket(bucket_key="signup|1|1.2.3.4",
                         window_start=int(time.time()), hits=1))
        await s.commit()

        removed = await rate_limit.purge_old_buckets(s, older_than=3600)
        await s.commit()
        assert removed == 1
        left = (await s.execute(select(RateBucket))).scalars().all()
        assert len(left) == 1


async def test_concurrent_requests_do_not_exceed_limit():
    """Параллельные запросы одного клиента не должны «проскочить» лимит.

    На SQLite проверяем счётчик в памяти: он общий для корутин одного
    процесса. Поведение общего счётчика в PostgreSQL обеспечивается
    атомарным INSERT ... ON CONFLICT DO UPDATE ... RETURNING и проверяется
    в CI на настоящей базе."""
    key = rate_limit.bucket_key("signup", 1, "9.9.9.9")

    async def one():
        await asyncio.sleep(0)
        return rate_limit.check_memory(key, 5, 60)[0]

    results = await asyncio.gather(*[one() for _ in range(20)])
    assert sum(results) == 5, results
