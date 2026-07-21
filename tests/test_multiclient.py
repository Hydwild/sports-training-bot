"""Тесты мультиклиента: токены клубов, роутинг ботов и отправок."""
import asyncio

from fastapi.testclient import TestClient

from app.main import app

H = {"x-admin-token": "tok"}


def test_tokens_endpoint():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "МК-1"},
                     headers=H).json()["id"]
        r = c.patch(f"/api/tenants/{tid}/tokens", headers=H,
                    json={"tg_token": "111:AAA", "vk_token": "vk1.a.zzz"})
        assert r.status_code == 200, r.text
        # без админ-токена нельзя
        r = c.patch(f"/api/tenants/{tid}/tokens", json={"tg_token": "x"})
        assert r.status_code == 401
        # очистка
        r = c.patch(f"/api/tenants/{tid}/tokens", headers=H,
                    json={"tg_token": ""})
        assert r.status_code == 200


def test_tg_client_bots_and_context():
    """Клиентские TG-боты поднимаются из базы; событие клиентского бота
    относится к его клубу."""
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "МК-TG"},
                     headers=H).json()["id"]
        c.patch(f"/api/tenants/{tid}/tokens", headers=H,
                json={"tg_token": "222:BBB"})

    async def run():
        from app.db.engine import engine
        await engine.dispose()
        from app.bots import telegram as tg
        # эмулируем поднятие клиентских ботов (кусок setup)
        from sqlalchemy import or_, select
        from app.core import bot_tokens
        from app.models.entities import Tenant
        from app.db.engine import SessionLocal
        from aiogram import Bot
        tg._tenant_bots.clear(); tg._token_tenants.clear()
        async with SessionLocal() as s:
            # токен хранится зашифрованным; открытая колонка — переходная
            tenants = list((await s.execute(select(Tenant).where(or_(
                Tenant.tg_token.is_not(None),
                Tenant.tg_token_enc != "")))).scalars())
        for t in tenants:
            tok = bot_tokens.token_of(t, "tg")
            if tok:
                tg._tenant_bots[t.id] = Bot(token=tok)
                tg._token_tenants[tok] = t.id
        assert tid in tg._tenant_bots
        # роутинг исходящих: клубу с токеном — его бот, прочим — дефолтный
        assert tg._bot_for(tid) is tg._tenant_bots[tid]
        assert tg._bot_for(999999) is tg._bot
        # контекст: событие клиентского бота -> его клуб
        token_ctx = tg._ctx_tenant.set(tid)
        try:
            async with SessionLocal() as s:
                rtid, is_admin = await tg._resolve_tenant(s, 12345, 777)
                assert rtid == tid and is_admin is False
        finally:
            tg._ctx_tenant.reset(token_ctx)
        for b in tg._tenant_bots.values():
            await b.session.close()
        tg._tenant_bots.clear(); tg._token_tenants.clear()

    asyncio.run(run())


def test_vk_send_routes_by_tenant():
    """Исходящее VK-сообщение клубу с собственным ботом идёт через его api."""
    from app.bots import vk

    sent = []

    class FakeMsgs:
        def __init__(self, tag):
            self.tag = tag

        async def send(self, **kw):
            sent.append((self.tag, kw["user_id"]))

    class FakeApi:
        def __init__(self, tag):
            self.messages = FakeMsgs(tag)

    class FakeBot:
        def __init__(self, tag):
            self.api = FakeApi(tag)

    old_bot = vk._bot
    old_map = dict(vk._api_by_tenant)
    try:
        vk._bot = FakeBot("default")
        vk._api_by_tenant.clear()
        vk._api_by_tenant[42] = FakeApi("client42")

        async def run():
            await vk._send(111, "hi", tenant_id=42)     # клиентский клуб
            await vk._send(222, "hi", tenant_id=1)      # клуб без своего бота
            await vk._send(333, "hi")                   # без клуба

        asyncio.run(run())
        assert sent == [("client42", 111), ("default", 222),
                        ("default", 333)], sent
    finally:
        vk._bot = old_bot
        vk._api_by_tenant.clear()
        vk._api_by_tenant.update(old_map)


def test_billing_suspend_and_web():
    """PATCH /billing: прошедшая дата приостанавливает клуб (веб-страница)."""
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "SaaS-клуб"},
                     headers=H).json()["id"]
        r = c.patch(f"/api/tenants/{tid}/billing", headers=H,
                    json={"paid_until": "2020-01-01"})
        assert r.status_code == 200
        r = c.get(f"/club/{tid}")
        assert "приостановлена" in r.text
        # неверный формат даты
        r = c.patch(f"/api/tenants/{tid}/billing", headers=H,
                    json={"paid_until": "01.01.2020"})
        assert r.status_code == 400
        # снятие ограничения возвращает работу
        c.patch(f"/api/tenants/{tid}/billing", headers=H,
                json={"paid_until": ""})
        assert "приостановлена" not in c.get(f"/club/{tid}").text


def test_signup_close_window_and_web_queueless():
    """Автозакрытие записи за N минут + web не копит сообщения в очереди."""
    import datetime as dt
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Закрытие-клуб"},
                     headers=H).json()["id"]

    async def run():
        from app.db.engine import SessionLocal
        from app.services.booking import BookingService
        from app.models.entities import Tenant, Outbox
        from sqlalchemy import select, func
        async with SessionLocal() as s:
            t = await s.get(Tenant, tid)
            t.signup_close_minutes = 120          # закрывать за 2 часа
            svc = BookingService(s, tid)
            tr = await svc.create_training(
                title="Скоро", location="З", max_participants=4,
                duration_min=60, state="published", publish_at=None,
                platform="tg", user_id=0,
                start_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=60))
            far = await svc.create_training(
                title="Нескоро", location="З", max_participants=4,
                duration_min=60, state="published", publish_at=None,
                platform="tg", user_id=0,
                start_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2))
            await s.commit()
            r1 = await svc.sign_up(tr.id, "tg", 5, "A")
            r2 = await svc.sign_up(far.id, "tg", 5, "A")
            assert r1.result == "closed", r1.result      # за 60 мин — закрыто
            assert r2.result == "active", r2.result      # далёкая — открыта
            # web-очередь: enqueue для web не пишет строк
            before = (await s.execute(select(func.count()).select_from(
                Outbox))).scalar()
            await svc.repo.enqueue("web", 123, "тест")
            await s.commit()
            after = (await s.execute(select(func.count()).select_from(
                Outbox))).scalar()
            assert before == after

    asyncio.run(run())


def test_hot_reload_registries():
    """reload_client_bots перечитывает токены из базы без рестарта."""
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "HotReload"},
                     headers=H).json()["id"]
        r = c.patch(f"/api/tenants/{tid}/tokens", headers=H,
                    json={"tg_token": "333:CCC"})
        assert r.status_code == 200

    async def run():
        from app.bots import telegram as tg
        await tg.reload_client_bots()
        assert tid in tg._tenant_bots
        assert tg._token_tenants.get("333:CCC") == tid
        # очистка токена убирает бота
        from app.db.engine import SessionLocal
        from app.models.entities import Tenant
        from app.core import bot_tokens
        async with SessionLocal() as s:
            t = await s.get(Tenant, tid)
            bot_tokens.set_token(t, "tg", "")
            await s.commit()
        await tg.reload_client_bots()
        assert tid not in tg._tenant_bots

    asyncio.run(run())


def test_security_fixes():
    """XSS через brand_color, формат TG-токена, неактивный клуб."""
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Сек-клуб"},
                     headers=H).json()["id"]

    async def setup_color():
        from app.db.engine import SessionLocal
        from app.models.entities import Tenant
        async with SessionLocal() as s:
            t = await s.get(Tenant, tid)
            t.brand_color = "</style><script>alert(1)</script>"
            await s.commit()

    asyncio.run(setup_color())
    with TestClient(app) as c:
        r = c.get(f"/club/{tid}")
        assert "<script>alert" not in r.text          # цвет обеззаражен
        assert "#3a7bd5" in r.text                    # применён дефолт
        # мусорный TG-токен отклоняется
        r = c.patch(f"/api/tenants/{tid}/tokens", headers=H,
                    json={"tg_token": "не-токен"})
        assert r.status_code == 400
        # деактивированный клуб недоступен публично
        async def deact():
            from app.db.engine import SessionLocal
            from app.models.entities import Tenant
            async with SessionLocal() as s:
                (await s.get(Tenant, tid)).is_active = False
                await s.commit()
        asyncio.run(deact())
        assert c.get(f"/club/{tid}").status_code == 404
