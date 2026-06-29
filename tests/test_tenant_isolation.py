"""Изоляция тенантов: клуб не видит данные другого клуба."""
import datetime as dt
import pytest
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService


async def _make_club(session, name):
    g = GlobalRepository(session)
    t = await g.create_tenant(name=name)
    await session.commit()
    return t.id


async def test_trainings_isolated(session):
    a = await _make_club(session, "A")
    b = await _make_club(session, "B")
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    svc_a = BookingService(session, a)
    tr = await svc_a.create_training(title="TA", start_at=now, location="",
                                     max_participants=5, platform="tg", user_id=1)
    svc_b = BookingService(session, b)
    assert await svc_b.repo.get_training(tr.id) is None  # B не видит тренировку A
    assert await svc_a.repo.get_training(tr.id) is not None


async def test_queue_and_promotion(session):
    a = await _make_club(session, "A")
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    svc = BookingService(session, a)
    tr = await svc.create_training(title="T", start_at=now, location="",
                                   max_participants=1, platform="tg", user_id=1)
    r1 = await svc.sign_up(tr.id, "tg", 100, "Аня")
    r2 = await svc.sign_up(tr.id, "vk", 200, "Боря")
    assert r1.result == "active" and r2.result == "queue"
    res = await svc.cancel_signup(tr.id, "tg", 100)
    assert res["promoted"].name == "Боря"
