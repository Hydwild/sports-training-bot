"""
Перенос записи (reschedule_signup), уведомления админам об отменах и
переносах, утренний дайджест записей на сегодня (_admin_daily_digest).

Изолированный in-memory движок (как в test_demo_reset) — не зависим от
порядка других тестов с общей файловой БД.
"""
import datetime as dt

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.entities import Base, Outbox
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService
from app.services import tasks


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


async def _mk_tenant(s, admin_tg=9001, **kw):
    g = GlobalRepository(s)
    t = await g.create_tenant(name="Клуб Переносов", admin_tg_id=admin_tg, **kw)
    await s.commit()
    return t


async def _mk_training(svc, title="Слот", days=1, maxp=5):
    return await svc.create_training(
        title=title,
        start_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days),
        location="", max_participants=maxp, platform="tg", user_id=0)


async def _admin_msgs(s, admin_tg=9001) -> list[str]:
    rows = list((await s.execute(select(Outbox).where(
        Outbox.user_id == admin_tg, Outbox.platform == "tg"))).scalars())
    return [r.text for r in rows]


# ---------- reschedule_signup ----------

async def test_reschedule_moves_signup_and_notifies_admin(maker):
    async with maker() as s:
        t = await _mk_tenant(s)
        svc = BookingService(s, t.id)
        a = await _mk_training(svc, "Утро")
        b = await _mk_training(svc, "Вечер", days=2)
        await svc.sign_up(a.id, "tg", 100, "Иван")
        res = await svc.reschedule_signup(a.id, b.id, "tg", 100)
        assert res["ok"] and res["result"] == "active"
        assert await svc.repo.get_user_signup(a.id, "tg", 100) is None
        assert (await svc.repo.get_user_signup(b.id, "tg", 100)).status == "active"
        msgs = await _admin_msgs(s)
        assert any("перенёс" in m and "Утро" in m and "Вечер" in m for m in msgs)
        # уведомление о переносе одно, отдельного "отменил" нет
        assert not any("отменил" in m for m in msgs)


async def test_reschedule_to_full_training_goes_to_queue(maker):
    async with maker() as s:
        t = await _mk_tenant(s)
        svc = BookingService(s, t.id)
        a = await _mk_training(svc, "Откуда")
        b = await _mk_training(svc, "Куда", days=2, maxp=1)
        await svc.sign_up(b.id, "tg", 555, "Занял")
        await svc.sign_up(a.id, "tg", 100, "Иван")
        res = await svc.reschedule_signup(a.id, b.id, "tg", 100)
        assert res["ok"] and res["result"] == "queue" and res["position"] == 1


async def test_reschedule_closed_target_keeps_original(maker):
    async with maker() as s:
        t = await _mk_tenant(s)
        svc = BookingService(s, t.id)
        a = await _mk_training(svc, "Исходный")
        b = await _mk_training(svc, "Отменённый", days=2)
        b.is_cancelled = True
        await s.commit()
        await svc.sign_up(a.id, "tg", 100, "Иван")
        res = await svc.reschedule_signup(a.id, b.id, "tg", 100)
        assert not res["ok"] and res["reason"] == "closed"
        # исходная запись цела
        assert (await svc.repo.get_user_signup(a.id, "tg", 100)) is not None


async def test_reschedule_locked_and_same_and_not_signed(maker):
    async with maker() as s:
        t = await _mk_tenant(s)
        svc = BookingService(s, t.id)
        soon = await svc.create_training(
            title="Скоро",
            start_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10),
            location="", max_participants=5, platform="tg", user_id=0)
        other = await _mk_training(svc, "Другой")
        await svc.sign_up(soon.id, "tg", 100, "Иван")
        res = await svc.reschedule_signup(soon.id, other.id, "tg", 100,
                                          lock_minutes=60)
        assert not res["ok"] and res["reason"] == "locked"
        assert not (await svc.reschedule_signup(soon.id, soon.id, "tg", 100))["ok"]
        res3 = await svc.reschedule_signup(other.id, soon.id, "tg", 777)
        assert res3["reason"] == "not_signed"


# ---------- уведомления об отмене ----------

async def test_cancel_notifies_admin_but_not_self(maker):
    async with maker() as s:
        t = await _mk_tenant(s)
        svc = BookingService(s, t.id)
        a = await _mk_training(svc, "Игра")
        await svc.sign_up(a.id, "tg", 100, "Пётр")
        await svc.cancel_signup(a.id, "tg", 100)
        msgs = await _admin_msgs(s)
        assert any("Пётр" in m and "отменил" in m and "Игра" in m for m in msgs)

    # админ отменяет свою запись — сам себе уведомление не получает
    async with maker() as s2:
        g = GlobalRepository(s2)
        t2 = await g.create_tenant(name="Клуб Сам", admin_tg_id=9100)
        await s2.commit()
        svc2 = BookingService(s2, t2.id)
        tr = await _mk_training(svc2, "Своя")
        await svc2.sign_up(tr.id, "tg", 9100, "Админ")
        await svc2.cancel_signup(tr.id, "tg", 9100)
        assert not await _admin_msgs(s2, admin_tg=9100)


# ---------- утренний дайджест ----------

def _tz_midday() -> str:
    """Таймзона, в которой прямо сейчас ~полдень: тесты дайджеста создают
    слот "+2 часа" и он гарантированно остаётся СЕГОДНЯШНИМ по местной
    дате клуба, в какое бы время суток тесты ни запускались (со стандартной
    Europe/Moscow слот у полуночи уезжал на завтра и дайджест "пустел")."""
    h = dt.datetime.now(dt.timezone.utc).hour
    off = (12 - h) % 24
    if off > 12:
        off -= 24
    if off == 0:
        return "UTC"
    # Etc/GMT-5 == UTC+5 (знак в названии инвертирован)
    return f"Etc/GMT-{off}" if off > 0 else f"Etc/GMT+{abs(off)}"


async def _mk_today_training(svc, title="Сегодня", hours_ahead=2, maxp=5):
    return await svc.create_training(
        title=title,
        start_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours_ahead),
        location="", max_participants=maxp, platform="tg", user_id=0)


async def test_digest_sent_once_per_day(monkeypatch, maker):
    monkeypatch.setattr(tasks, "SessionLocal", maker)
    monkeypatch.setattr(tasks, "DIGEST_HOUR", 0)  # не зависим от часа теста
    async with maker() as s:
        t = await _mk_tenant(s, timezone=_tz_midday())
        svc = BookingService(s, t.id)
        tr = await _mk_today_training(svc)
        await svc.sign_up(tr.id, "tg", 1, "А")
        await svc.sign_up(tr.id, "tg", 2, "Б")
        await s.commit()

    await tasks._admin_daily_digest()
    await tasks._admin_daily_digest()  # повтор в тот же день — без дублей

    async with maker() as s:
        msgs = [m for m in await _admin_msgs(s) if "Записи на сегодня" in m]
        assert len(msgs) == 1
        assert "Сегодня" in msgs[0] and "2/5" in msgs[0]
        assert "Всего записано: 2" in msgs[0]


async def test_digest_skips_day_without_trainings(monkeypatch, maker):
    monkeypatch.setattr(tasks, "SessionLocal", maker)
    monkeypatch.setattr(tasks, "DIGEST_HOUR", 0)
    async with maker() as s:
        await _mk_tenant(s)
    await tasks._admin_daily_digest()
    async with maker() as s:
        assert not [m for m in await _admin_msgs(s)
                    if "Записи на сегодня" in m]
        # но день помечен — завтра снова проверим, сегодня не дёргаем
        from app.models.entities import Tenant
        t = (await s.execute(select(Tenant))).scalars().first()
        assert t.last_digest_date != ""


async def test_digest_skips_demo_and_before_hour(monkeypatch, maker):
    monkeypatch.setattr(tasks, "SessionLocal", maker)
    monkeypatch.setattr(tasks, "DIGEST_HOUR", 0)
    async with maker() as s:
        t = await _mk_tenant(s, is_demo=True, timezone=_tz_midday())
        svc = BookingService(s, t.id)
        tr = await _mk_today_training(svc)
        await svc.sign_up(tr.id, "tg", 1, "Демо")
        await s.commit()
    await tasks._admin_daily_digest()
    async with maker() as s:
        assert not [m for m in await _admin_msgs(s)
                    if "Записи на сегодня" in m]

    # до DIGEST_HOUR не отправляем и день не помечаем
    monkeypatch.setattr(tasks, "DIGEST_HOUR", 24)
    async with maker() as s2:
        g = GlobalRepository(s2)
        t2 = await g.create_tenant(name="Ранний", admin_tg_id=9200,
                                   timezone=_tz_midday())
        await s2.commit()
        svc2 = BookingService(s2, t2.id)
        await _mk_today_training(svc2)
        await s2.commit()
    await tasks._admin_daily_digest()
    async with maker() as s2:
        from app.models.entities import Tenant
        t2 = (await s2.execute(select(Tenant).where(
            Tenant.admin_tg_id == 9200))).scalars().first()
        assert t2.last_digest_date == ""
