"""Настройки клуба: окно отмены, авто-истечение гостей, флаг уведомления публикации."""
import datetime as dt
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService


async def _club(session, **tenant_kwargs):
    g = GlobalRepository(session)
    t = await g.create_tenant(name="Клуб", admin_tg_id=1, **tenant_kwargs)
    await session.commit()
    return t


async def test_cancel_lock_blocks_late_cancel(session):
    t = await _club(session)
    svc = BookingService(session, t.id)
    # тренировка через 30 минут
    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
    tr = await svc.create_training(title="T", start_at=soon, location="",
                                   max_participants=5, platform="tg", user_id=1)
    await svc.sign_up(tr.id, "tg", 100, "Аня")
    # окно отмены 60 мин -> отписка запрещена (до начала 30 < 60)
    res = await svc.cancel_signup(tr.id, "tg", 100, lock_minutes=60)
    assert res["cancelled"] is False and res.get("locked") is True
    # участник остался записан
    assert await svc.repo.get_user_signup(tr.id, "tg", 100) is not None


async def test_cancel_allowed_when_far(session):
    t = await _club(session)
    svc = BookingService(session, t.id)
    far = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5)
    tr = await svc.create_training(title="T", start_at=far, location="",
                                   max_participants=5, platform="tg", user_id=1)
    await svc.sign_up(tr.id, "tg", 100, "Аня")
    res = await svc.cancel_signup(tr.id, "tg", 100, lock_minutes=60)
    assert res["cancelled"] is True


async def test_publish_without_notify(session):
    t = await _club(session)
    svc = BookingService(session, t.id)
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    tr = await svc.create_training(title="T", start_at=now, location="",
                                   max_participants=5, platform="tg", user_id=1,
                                   state="draft")
    # подписчик есть
    await svc.repo.upsert_subscriber("tg", 100, "Аня")
    await session.commit()
    await svc.publish_training(tr.id, notify=False)
    g = GlobalRepository(session)
    # уведомлений об открытии не создано
    assert len(await g.fetch_pending_outbox("tg")) == 0


async def test_settings_defaults(session):
    t = await _club(session)
    # значения по умолчанию из модели
    assert t.reminder_enabled is True
    assert t.reminder_minutes == 60
    assert t.guest_reminder_minutes == 120
    assert t.guest_expire_enabled is False
    assert t.cancel_lock_minutes == 0
    assert t.publish_notify_enabled is True
