"""
Разбор недоставленных уведомлений оператором.

Раньше провал доставки был виден только в логах: сообщение помечалось
недоставленным, и дальше о нём никто не вспоминал — человек просто не
получал напоминание, и никто об этом не знал.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

H = {"x-admin-token": "tok"}


@pytest.fixture(autouse=True)
def _clean():
    from app.api import rate_limit
    rate_limit._memory.clear()
    yield
    rate_limit._memory.clear()


def _login(c):
    r = c.post("/admin/platform/login", data={"token": "tok"},
               follow_redirects=False)
    c.cookies.set("platform_token", r.cookies["platform_token"])


def _csrf(text):
    return re.search(r'name="csrf" value="([^"]+)"', text).group(1)


def _dead_message(c, tenant_name="Клуб Очереди", error="бот заблокирован"):
    import asyncio

    tid = c.post("/api/tenants", json={"name": tenant_name},
                 headers=H).json()["id"]

    async def seed():
        from app.db.engine import SessionLocal, engine
        from app.models.entities import Outbox
        await engine.dispose()
        async with SessionLocal() as s:
            row = Outbox(tenant_id=tid, platform="tg", user_id=42,
                         text="Напоминание: занятие завтра, Мария",
                         sent=True, status="dead", attempts=5,
                         last_error=error)
            s.add(row)
            await s.commit()
            return row.id

    return tid, asyncio.run(seed())


def test_operator_sees_dead_without_message_text():
    with TestClient(app) as c:
        tid, mid = _dead_message(c)
        _login(c)
        page = c.get("/admin/platform/outbox")
        assert page.status_code == 200
        assert str(mid) in page.text
        assert "бот заблокирован" in page.text          # причина видна
        assert "Мария" not in page.text                 # текст — нет


def test_retry_returns_message_to_queue():
    import asyncio

    with TestClient(app) as c:
        _tid, mid = _dead_message(c, tenant_name="Клуб Повтора")
        _login(c)
        page = c.get("/admin/platform/outbox")
        r = c.post(f"/admin/platform/outbox/{mid}/retry",
                   data={"csrf": _csrf(page.text)}, follow_redirects=False)
        assert r.status_code == 303

        async def state():
            from app.db.engine import SessionLocal, engine
            from app.models.entities import Outbox
            await engine.dispose()
            async with SessionLocal() as s:
                row = await s.get(Outbox, mid)
                return row.status, row.attempts

        status, attempts = asyncio.run(state())
        assert status == "pending"
        assert attempts == 0, "счётчик попыток не обнулён — снова упадёт в dead"


def test_discard_marks_handled():
    import asyncio

    with TestClient(app) as c:
        _tid, mid = _dead_message(c, tenant_name="Клуб Отброса")
        _login(c)
        page = c.get("/admin/platform/outbox")
        c.post(f"/admin/platform/outbox/{mid}/discard",
               data={"csrf": _csrf(page.text)}, follow_redirects=False)

        async def state():
            from app.db.engine import SessionLocal, engine
            from app.models.entities import Outbox
            await engine.dispose()
            async with SessionLocal() as s:
                row = await s.get(Outbox, mid)
                return row.status, row.handled_at

        status, handled = asyncio.run(state())
        assert status == "discarded"
        assert handled is not None       # по ней работает суточная чистка


def test_actions_need_csrf_and_auth():
    with TestClient(app) as c:
        _tid, mid = _dead_message(c, tenant_name="Клуб Защиты")
        # без входа
        c.cookies.clear()
        assert c.get("/admin/platform/outbox",
                     follow_redirects=False).status_code in (302, 303, 401, 403)
        # со входом, но без csrf
        _login(c)
        assert c.post(f"/admin/platform/outbox/{mid}/retry").status_code == 403


def test_filters_narrow_the_list():
    with TestClient(app) as c:
        tid_a, mid_a = _dead_message(c, tenant_name="Клуб Фильтра А")
        _tid_b, mid_b = _dead_message(c, tenant_name="Клуб Фильтра Б")
        _login(c)
        page = c.get(f"/admin/platform/outbox?tenant_id={tid_a}").text
        assert str(mid_a) in page
        assert f">{mid_b}</td>" not in page


def test_health_summary_is_shown():
    with TestClient(app) as c:
        _dead_message(c, tenant_name="Клуб Сводки")
        _login(c)
        page = c.get("/admin/platform/outbox").text
        assert "Всего недоставленных" in page
        assert "ждёт" in page
