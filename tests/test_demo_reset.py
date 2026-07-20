"""
Ночной сброс демо-клубов (tasks._demo_reset_daily): демо-клуб (Tenant.is_demo)
полностью пересобирается раз в сутки — старые тренировки/записи/роли
удаляются, создаётся свежий набор примеров. Обычные клубы не затрагиваются.

Изолированный in-memory движок (как в test_outbox_retry.py), с тем же
PRAGMA foreign_keys=ON, что и в app/db/engine.py — иначе каскадное удаление
Signup/Payment при удалении Training тут не сработает и тест соврёт.
"""
import datetime as dt

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.entities import Base, Membership, Outbox, Signup, Training
from app.repositories.repo import GlobalRepository, TenantRepository
from app.services.booking import BookingService
from app.services import tasks


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


async def _mk_demo_tenant_with_data(maker) -> int:
    async with maker() as s:
        g = GlobalRepository(s)
        t = await g.create_tenant(name="Демо-клуб", is_demo=True)
        await s.commit()
        svc = BookingService(s, t.id)
        tr = await svc.create_training(
            title="Старая тренировка",
            start_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1),
            location="Зал", max_participants=5, platform="tg", user_id=0)
        await svc.sign_up(tr.id, "tg", 42, "Иван")
        await TenantRepository(s, t.id).upsert_membership(999, "coach", "Демо-тренер")
        await TenantRepository(s, t.id).enqueue("tg", 42, "тест")
        await s.commit()
        return t.id


async def _mk_regular_tenant_with_data(maker) -> int:
    async with maker() as s:
        g = GlobalRepository(s)
        t = await g.create_tenant(name="Обычный клуб", is_demo=False)
        await s.commit()
        svc = BookingService(s, t.id)
        tr = await svc.create_training(
            title="Настоящая тренировка",
            start_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1),
            location="Зал", max_participants=5, platform="tg", user_id=0)
        await svc.sign_up(tr.id, "tg", 42, "Иван")
        await s.commit()
        return t.id


async def test_demo_tenant_reset_wipes_and_reseeds(monkeypatch, maker):
    monkeypatch.setattr(tasks, "SessionLocal", maker)
    tid = await _mk_demo_tenant_with_data(maker)

    last_day = [None]
    await tasks._demo_reset_daily(last_day)

    async with maker() as s:
        trainings = list((await s.execute(
            select(Training).where(Training.tenant_id == tid))).scalars())
        assert len(trainings) == len(tasks._DEMO_SEED)
        assert {t.title for t in trainings} == {i["title"] for i in tasks._DEMO_SEED}

        assert not list((await s.execute(
            select(Membership).where(Membership.tenant_id == tid))).scalars())
        assert not list((await s.execute(
            select(Outbox).where(Outbox.tenant_id == tid))).scalars())
        # Signup со старой (уже удалённой) тренировки должен уйти каскадом
        assert not list((await s.execute(select(Signup))).scalars())

    assert last_day[0] == dt.date.today().isoformat()


async def test_regular_tenant_untouched_by_demo_reset(monkeypatch, maker):
    monkeypatch.setattr(tasks, "SessionLocal", maker)
    tid = await _mk_regular_tenant_with_data(maker)

    last_day = [None]
    await tasks._demo_reset_daily(last_day)

    async with maker() as s:
        trainings = list((await s.execute(
            select(Training).where(Training.tenant_id == tid))).scalars())
        assert len(trainings) == 1
        assert trainings[0].title == "Настоящая тренировка"
        signups = list((await s.execute(select(Signup))).scalars())
        assert len(signups) == 1  # запись реального клуба не тронута


async def test_demo_reset_runs_once_per_day(monkeypatch, maker):
    monkeypatch.setattr(tasks, "SessionLocal", maker)
    tid = await _mk_demo_tenant_with_data(maker)

    last_day = [None]
    await tasks._demo_reset_daily(last_day)
    await tasks._demo_reset_daily(last_day)  # тот же день — не должно дублировать

    async with maker() as s:
        trainings = list((await s.execute(
            select(Training).where(Training.tenant_id == tid))).scalars())
        assert len(trainings) == len(tasks._DEMO_SEED)


async def test_demo_reset_skips_without_demo_tenants(monkeypatch, maker):
    monkeypatch.setattr(tasks, "SessionLocal", maker)
    last_day = [None]
    await tasks._demo_reset_daily(last_day)  # не должно падать при пустой базе
    assert last_day[0] == dt.date.today().isoformat()
