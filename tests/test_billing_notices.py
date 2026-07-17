"""
Уведомления клиентам (тренерам/владельцам клуба) об истекающей/истёкшей
подписке — в их собственном боте, не только сводка оператору площадки.
См. app/services/tasks.py: _daily_maintenance.
"""
import asyncio
import datetime as dt

from fastapi.testclient import TestClient

from app.main import app

H = {"x-admin-token": "tok"}


def _mk_tenant(c, name, paid_until, admin_tg_id=None, admin_vk_id=None):
    body = {"name": name}
    if admin_tg_id is not None:
        body["admin_tg_id"] = admin_tg_id
    if admin_vk_id is not None:
        body["admin_vk_id"] = admin_vk_id
    tid = c.post("/api/tenants", json=body, headers=H).json()["id"]
    c.patch(f"/api/tenants/{tid}/billing", headers=H,
           json={"paid_until": paid_until})
    return tid


async def _run_maintenance():
    from app.db.engine import engine
    from app.services import tasks
    await engine.dispose()   # сбрасываем пул из чужого event loop
    await tasks._daily_maintenance([None])   # last_day=[None] -> всегда выполняется


def test_client_notified_when_expiring_soon():
    with TestClient(app) as c:
        tid = _mk_tenant(c, "Клуб Скоро",
                         (dt.date.today() + dt.timedelta(days=2)).isoformat(),
                         admin_tg_id=4201)

    async def run():
        from app.db.engine import SessionLocal
        from app.repositories.repo import GlobalRepository
        await _run_maintenance()
        async with SessionLocal() as s:
            g = GlobalRepository(s)
            pending = await g.fetch_pending_outbox("tg")
            mine = [p for p in pending if p.user_id == 4201]
            assert len(mine) == 1
            assert "истекает" in mine[0].text
            t = await g.get_tenant(tid)
            assert t.last_billing_notice.endswith(":soon")

    asyncio.run(run())


def test_client_notified_when_expired():
    with TestClient(app) as c:
        tid = _mk_tenant(c, "Клуб Истёк",
                         (dt.date.today() - dt.timedelta(days=1)).isoformat(),
                         admin_tg_id=4202)

    async def run():
        from app.db.engine import SessionLocal
        from app.repositories.repo import GlobalRepository
        await _run_maintenance()
        async with SessionLocal() as s:
            g = GlobalRepository(s)
            pending = await g.fetch_pending_outbox("tg")
            mine = [p for p in pending if p.user_id == 4202]
            assert len(mine) == 1
            assert "истекла" in mine[0].text
            assert "приостановлен" in mine[0].text
            t = await g.get_tenant(tid)
            assert t.last_billing_notice.endswith(":expired")

    asyncio.run(run())


def test_no_duplicate_notice_on_second_run_same_state():
    """Повторный прогон в тот же день (или на след. день без изменений)
    не должен слать сообщение повторно — только один раз на стадию."""
    with TestClient(app) as c:
        _mk_tenant(c, "Клуб Дубль",
                  (dt.date.today() - dt.timedelta(days=1)).isoformat(),
                  admin_tg_id=4203)

    async def run():
        from app.db.engine import SessionLocal
        from app.repositories.repo import GlobalRepository
        await _run_maintenance()
        await _run_maintenance()  # второй прогон — маркер уже совпадает
        async with SessionLocal() as s:
            g = GlobalRepository(s)
            pending = await g.fetch_pending_outbox("tg")
            mine = [p for p in pending if p.user_id == 4203]
            assert len(mine) == 1, "сообщение задублировалось"

    asyncio.run(run())


def test_notice_resumes_after_renewal_and_new_expiry():
    """Продление оплаты (новый paid_until) естественным образом сбрасывает
    маркер — при повторном приближении даты уведомление придёт снова."""
    with TestClient(app) as c:
        tid = _mk_tenant(c, "Клуб Продление",
                         (dt.date.today() - dt.timedelta(days=1)).isoformat(),
                         admin_tg_id=4204)

    async def run():
        from app.db.engine import SessionLocal
        from app.repositories.repo import GlobalRepository
        await _run_maintenance()   # первое уведомление ("expired")

        async with SessionLocal() as s:
            g = GlobalRepository(s)
            t = await g.get_tenant(tid)
            assert t.last_billing_notice.endswith(":expired")
            # оператор продлевает клуб на новую дату (тоже "скоро истекает")
            t.paid_until = (dt.date.today() + dt.timedelta(days=1)).isoformat()
            await s.commit()

        await _run_maintenance()   # маркер устарел -> новое уведомление ("soon")
        async with SessionLocal() as s:
            g = GlobalRepository(s)
            pending = await g.fetch_pending_outbox("tg")
            mine = [p for p in pending if p.user_id == 4204]
            assert len(mine) == 2   # "expired" + "soon" после продления
            assert mine[1].text != mine[0].text
            t = await g.get_tenant(tid)
            assert t.last_billing_notice.endswith(":soon")

    asyncio.run(run())


def test_no_admin_id_no_crash_no_message():
    with TestClient(app) as c:
        tid = _mk_tenant(c, "Клуб Без Тренера",
                        (dt.date.today() - dt.timedelta(days=1)).isoformat())

    async def run():
        from app.db.engine import SessionLocal
        from app.repositories.repo import GlobalRepository
        await _run_maintenance()  # не должно упасть
        async with SessionLocal() as s:
            g = GlobalRepository(s)
            pending = await g.fetch_pending_outbox("tg")
            # для этого клуба некому слать (нет admin_tg_id/admin_vk_id)
            assert not [p for p in pending if p.tenant_id == tid]
            t = await g.get_tenant(tid)
            assert t.last_billing_notice == ""  # маркер не проставлялся

    asyncio.run(run())


def test_vk_admin_also_notified():
    with TestClient(app) as c:
        _mk_tenant(c, "Клуб ВК",
                  (dt.date.today() - dt.timedelta(days=1)).isoformat(),
                  admin_vk_id=9001)

    async def run():
        from app.db.engine import SessionLocal
        from app.repositories.repo import GlobalRepository
        await _run_maintenance()
        async with SessionLocal() as s:
            g = GlobalRepository(s)
            pending = await g.fetch_pending_outbox("vk")
            mine = [p for p in pending if p.user_id == 9001]
            assert len(mine) == 1
            assert "истекла" in mine[0].text

    asyncio.run(run())


def test_support_contact_included_when_configured(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "platform_support_contact", "@my_support")
    with TestClient(app) as c:
        _mk_tenant(c, "Клуб Контакт",
                  (dt.date.today() - dt.timedelta(days=1)).isoformat(),
                  admin_tg_id=4205)

    async def run():
        from app.db.engine import SessionLocal
        from app.repositories.repo import GlobalRepository
        await _run_maintenance()
        async with SessionLocal() as s:
            g = GlobalRepository(s)
            pending = await g.fetch_pending_outbox("tg")
            mine = [p for p in pending if p.user_id == 4205]
            assert "@my_support" in mine[0].text

    asyncio.run(run())
