"""
Тесты, которым нужна НАСТОЯЩАЯ PostgreSQL. Помечены `pg` и запускаются
только в CI (`pytest -m pg`) на живой базе: на SQLite они проверяли бы не
тот механизм — SKIP LOCKED и ON CONFLICT там работают иначе или никак.

Локально по умолчанию пропускаются (см. conftest: маркер не собирается без
переменной окружения PG-базы).
"""
import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.entities import Base, Outbox, RateBucket, Tenant
from app.repositories.repo import GlobalRepository

pytestmark = pytest.mark.pg

PG_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/ci")


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine(PG_URL)
    async with engine.begin() as conn:
        # своя изолированная схема на прогон, чтобы не мешать миграциям CI
        await conn.exec_driver_sql("DROP SCHEMA IF EXISTS pgtest CASCADE")
        await conn.exec_driver_sql("CREATE SCHEMA pgtest")
        await conn.exec_driver_sql("SET search_path TO pgtest")
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP SCHEMA IF EXISTS pgtest CASCADE")
    await engine.dispose()


async def _seed(maker, n: int) -> int:
    async with maker() as s:
        await s.execute(__import__("sqlalchemy").text("SET search_path TO pgtest"))
        t = Tenant(name="Клуб")
        s.add(t)
        await s.flush()
        for i in range(n):
            s.add(Outbox(tenant_id=t.id, platform="tg", user_id=i,
                         text=f"msg {i}", status="pending"))
        await s.commit()
        return t.id


async def test_two_workers_never_claim_the_same_message(maker):
    """Ключевая гарантия: параллельные воркеры не должны захватить одно и
    то же сообщение — иначе оно уйдёт получателю дважды."""
    await _seed(maker, 40)

    async def worker() -> list[int]:
        claimed_ids: list[int] = []
        async with maker() as s:
            await s.execute(__import__("sqlalchemy").text(
                "SET search_path TO pgtest"))
            g = GlobalRepository(s)
            while True:
                batch = await g.claim_pending_outbox("tg", limit=5)
                if not batch:
                    break
                claimed_ids += [o.id for o in batch]
                await s.commit()
        return claimed_ids

    results = await asyncio.gather(*[worker() for _ in range(4)])
    all_ids = [i for r in results for i in r]
    # каждое сообщение захвачено ровно один раз
    assert len(all_ids) == len(set(all_ids)), "одно сообщение захвачено дважды"
    assert len(all_ids) == 40


async def test_shared_rate_limit_is_atomic_under_concurrency(maker):
    """Общий счётчик: 20 параллельных запросов одного клиента дают ровно
    limit разрешений, не больше."""
    from app.api import rate_limit

    async def one() -> bool:
        async with maker() as s:
            await s.execute(__import__("sqlalchemy").text(
                "SET search_path TO pgtest"))
            ok, _retry = await rate_limit.check_shared(
                s, "signup|1|1.2.3.4", limit=5, window=60)
            return ok

    results = await asyncio.gather(*[one() for _ in range(20)])
    assert sum(results) == 5, results

    async with maker() as s:
        await s.execute(__import__("sqlalchemy").text("SET search_path TO pgtest"))
        rows = (await s.execute(select(RateBucket))).scalars().all()
        assert len(rows) == 1
        assert rows[0].hits == 20     # инкремент не потерял ни одного
