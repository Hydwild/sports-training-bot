"""Управление тренировкой: редактирование, повтор, следующая тренировка."""
import datetime as dt
import pytest
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService


async def _club_t(session, **kw):
    g = GlobalRepository(session)
    t = await g.create_tenant(name="Клуб")
    await session.commit()
    svc = BookingService(session, t.id)
    base = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)
    tr = await svc.create_training(title="T", start_at=base, location="Зал",
                                   max_participants=4, duration_min=120,
                                   platform="tg", user_id=1, **kw)
    return svc, tr


async def test_update_location_and_max(session):
    svc, tr = await _club_t(session)
    await svc.update_field(tr.id, "location", "Новый зал")
    await svc.update_field(tr.id, "max_participants", 10)
    t2 = await svc.repo.get_training(tr.id)
    assert t2.location == "Новый зал"
    assert t2.max_participants == 10


async def test_repeat_training(session):
    svc, tr = await _club_t(session)
    copy = await svc.repeat_training(tr.id, days_ahead=7)
    assert copy.id != tr.id
    assert copy.title == tr.title
    assert (copy.start_at - tr.start_at).days == 7


async def test_next_training_for_user(session):
    svc, tr = await _club_t(session)
    # не записан — None
    assert await svc.next_training_for_user("tg", 555) is None
    # записываем — находим
    await svc.sign_up(tr.id, "tg", 555, "Игрок")
    found = await svc.next_training_for_user("tg", 555)
    assert found is not None and found.id == tr.id


async def test_update_max_promotes_from_queue(session):
    svc, tr = await _club_t(session)
    await svc.update_field(tr.id, "max_participants", 1)
    await svc.sign_up(tr.id, "tg", 1, "A")
    await svc.sign_up(tr.id, "tg", 2, "B")  # в очередь
    # расширяем лимит — B должен подняться
    await svc.update_field(tr.id, "max_participants", 5)
    active = await svc.repo.get_signups(tr.id, "active")
    assert len(active) == 2
