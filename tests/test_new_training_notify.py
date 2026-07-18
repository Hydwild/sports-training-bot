"""
Личное уведомление подписчикам о новой тренировке при создании "сейчас".

Регресс: при создании тренировки через VK ("сейчас") подписчики уже
получали личное уведомление в outbox — а через Telegram нет, только пост
в группу/на стену ВК (видны лишь тем, кто состоит в группе/паблике).
Участник, просто писавший боту в личку, узнавал о новой тренировке
только сам открыв «🏸 Тренировки». booking.notify_new_training() —
общий переиспользуемый путь для обеих платформ.
"""
import datetime as dt

from fastapi.testclient import TestClient

import app.bots.telegram as tg
from app.main import app
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService

H = {"x-admin-token": "tok"}


async def test_notify_new_training_enqueues_all_subscribers(session):
    g = GlobalRepository(session)
    t = await g.create_tenant(name="Клуб Уведомлений")
    await session.commit()
    svc = BookingService(session, t.id)
    await svc.repo.upsert_subscriber("tg", 100, "Аня")
    await svc.repo.upsert_subscriber("vk", 200, "Боря")
    await session.commit()

    training = await svc.create_training(
        title="Игра", start_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1),
        location="Зал", max_participants=5, platform="tg", user_id=0)
    await session.commit()

    count = await svc.notify_new_training(training)
    assert count == 2

    from sqlalchemy import select
    from app.models.entities import Outbox
    rows = list((await session.execute(
        select(Outbox).where(Outbox.tenant_id == t.id))).scalars())
    assert len(rows) == 2
    assert any(r.platform == "tg" and r.user_id == 100 for r in rows)
    assert any(r.platform == "vk" and r.user_id == 200 for r in rows)
    assert all("Игра" in r.text for r in rows)


def test_creating_training_now_notifies_subscribers():
    """Сквозная проверка хелпера telegram._notify_subscribers_new_training,
    который вызывается при создании тренировки 'сейчас' через Telegram."""
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб Сейчас"},
                     headers=H).json()["id"]
        train = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
            "title": "Тренировка",
            "start_at": (dt.datetime.now(dt.timezone.utc)
                        + dt.timedelta(days=1)).isoformat(),
            "max_participants": 5,
        }).json()
        train_id = train["id"]

    import asyncio

    async def run():
        from app.db.engine import SessionLocal, engine
        from sqlalchemy import select
        from app.models.entities import Outbox
        await engine.dispose()
        # подписчик клуба (tg), не участвующий в самом создании тренировки —
        # web не годится: enqueue("web", ...) намеренно ничего не кладёт,
        # доставлять веб-участникам в личку некуда
        async with SessionLocal() as s:
            svc = BookingService(s, tid)
            await svc.repo.upsert_subscriber("tg", 500, "Подписчик")
            await s.commit()

        await tg._notify_subscribers_new_training(tid, train_id)
        async with SessionLocal() as s:
            rows = list((await s.execute(
                select(Outbox).where(Outbox.tenant_id == tid))).scalars())
            assert any(r.platform == "tg" and r.user_id == 500
                      and "Тренировка" in r.text for r in rows)

    asyncio.run(run())


def test_notify_subscribers_missing_training_does_not_raise():
    """Тренировка удалена/не найдена — тихо ничего не делает, не падает."""
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб Нет Тренировки"},
                     headers=H).json()["id"]

    import asyncio
    asyncio.run(tg._notify_subscribers_new_training(tid, 999999))
