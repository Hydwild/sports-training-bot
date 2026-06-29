"""Подсказки при создании: недавние места и времена по дню недели."""
import datetime as dt
import pytest
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService


async def _club(session):
    g = GlobalRepository(session)
    t = await g.create_tenant(name="Клуб")
    await session.commit()
    return t.id


async def test_recent_locations(session):
    tid = await _club(session)
    svc = BookingService(session, tid)
    base = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    await svc.create_training(title="A", start_at=base, location="СГАУ",
                              max_participants=5, platform="tg", user_id=1)
    await svc.create_training(title="B", start_at=base, location="ЧСАА",
                              max_participants=5, platform="tg", user_id=1)
    await svc.create_training(title="C", start_at=base, location="СГАУ",
                              max_participants=5, platform="tg", user_id=1)
    places = await svc.recent_locations()
    # СГАУ и ЧСАА, без дублей, свежие первыми
    assert "СГАУ" in places and "ЧСАА" in places
    assert len(places) == len(set(places))  # без повторов


async def test_times_for_weekday(session):
    tid = await _club(session)
    svc = BookingService(session, tid)
    # создаём тренировку в конкретный день недели и время
    # берём ближайшую среду
    tz = svc.tz
    now = dt.datetime.now(tz)
    days_ahead = (2 - now.weekday()) % 7 or 7   # среда = 2
    wed = (now + dt.timedelta(days=days_ahead)).replace(
        hour=19, minute=0, second=0, microsecond=0)
    await svc.create_training(title="Ср", start_at=wed, location="Зал",
                              max_participants=5, platform="tg", user_id=1)
    times = await svc.times_for_weekday(2)  # среда
    assert "19:00" in times
    # для другого дня (понедельник) — пусто
    assert await svc.times_for_weekday(0) == []
