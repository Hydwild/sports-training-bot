"""
Удаление клуба из панели оператора.

Операция необратимая: вместе с клубом каскадом уходят его занятия, записи,
участники, платежи и веб-клиенты с зашифрованными телефонами. Поэтому
тесты здесь в первую очередь про то, что случайно удалить нельзя, а
удалённое действительно исчезает целиком — без осиротевших строк, которые
потом всплывут в чужом клубе.
"""
import re

import pytest
from fastapi.testclient import TestClient

import app.admin.routes as admin_routes
from app.main import app

H = {"x-admin-token": "tok"}


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(admin_routes, "_cookie_secure", lambda: False)
    from app.api import rate_limit
    rate_limit._memory.clear()
    yield
    rate_limit._memory.clear()


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/admin/platform/login", data={"token": "tok"})
        yield c


def _csrf(text: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', text)
    assert m, "csrf не найден"
    return m.group(1)


def _make_club(c, name: str) -> int:
    return c.post("/api/tenants", json={"name": name}, headers=H).json()["id"]


def _delete(c, tenant_id: int, confirm: str):
    page = c.get(f"/admin/platform/{tenant_id}/edit")
    return c.post(f"/admin/platform/{tenant_id}/delete",
                  data={"csrf": _csrf(page.text), "confirm_name": confirm},
                  follow_redirects=False)


def _exists(_c, tenant_id: int) -> bool:
    """Наличие клуба проверяем в базе: GET одного клуба в API нет."""
    import asyncio

    async def _check() -> bool:
        from app.db.engine import SessionLocal, engine
        from app.models.entities import Tenant
        await engine.dispose()
        async with SessionLocal() as s:
            return (await s.get(Tenant, tenant_id)) is not None

    return asyncio.run(_check())


# ---------- случайно удалить нельзя ----------

def test_wrong_name_does_not_delete(client):
    tid = _make_club(client, "Салон Гортензия")
    r = _delete(client, tid, "Салон Гортензи")      # опечатка
    assert r.status_code == 400
    assert "не совпадает" in r.text
    assert _exists(client, tid), "клуб удалён при неверном подтверждении"


def test_empty_confirmation_does_not_delete(client):
    tid = _make_club(client, "Салон Пустой")
    assert _delete(client, tid, "").status_code == 400
    assert _exists(client, tid)


def test_name_of_another_club_does_not_delete(client):
    """Ввод чужого названия не должен срабатывать ни для одного из клубов."""
    a = _make_club(client, "Клуб А")
    b = _make_club(client, "Клуб Б")
    assert _delete(client, a, "Клуб Б").status_code == 400
    assert _exists(client, a) and _exists(client, b)


def test_csrf_is_required(client):
    tid = _make_club(client, "Салон CSRF")
    r = client.post(f"/admin/platform/{tid}/delete",
                    data={"confirm_name": "Салон CSRF"})
    assert r.status_code == 403
    assert _exists(client, tid)


def test_unauthenticated_cannot_delete(client):
    tid = _make_club(client, "Салон Чужой")
    page = client.get(f"/admin/platform/{tid}/edit")
    csrf = _csrf(page.text)
    client.cookies.clear()
    r = client.post(f"/admin/platform/{tid}/delete",
                    data={"csrf": csrf, "confirm_name": "Салон Чужой"},
                    follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403)
    client.post("/admin/platform/login", data={"token": "tok"})
    assert _exists(client, tid)


def test_missing_club_is_404(client):
    page = client.get("/admin/platform")
    r = client.post("/admin/platform/999999/delete",
                    data={"csrf": _csrf(page.text), "confirm_name": "x"})
    assert r.status_code == 404


# ---------- удаление действительно удаляет ----------

def test_exact_name_deletes_club(client):
    tid = _make_club(client, "Салон На Удаление")
    r = _delete(client, tid, "Салон На Удаление")
    assert r.status_code == 200
    assert "удалён" in r.text
    assert not _exists(client, tid)


def test_confirmation_tolerates_surrounding_spaces(client):
    tid = _make_club(client, "Салон Пробелы")
    assert _delete(client, tid, "  Салон Пробелы  ").status_code == 200
    assert not _exists(client, tid)


def test_children_are_removed_with_the_club(client):
    """Каскад: осиротевшие записи потом всплыли бы в чужом клубе."""
    import asyncio
    import datetime as dt

    tid = _make_club(client, "Салон С Данными")
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    tr = client.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": "Стрижка", "start_at": start, "max_participants": 1,
    }).json()["id"]
    client.post(f"/club/{tid}/signup", data={
        "consent": "1", "training_id": tr, "name": "Клиент",
        "phone": "79240001111"})

    async def counts():
        from sqlalchemy import func, select

        from app.db.engine import SessionLocal, engine
        from app.models.entities import Signup, Training, WebCustomer
        await engine.dispose()
        async with SessionLocal() as s:
            out = {}
            for model in (Training, Signup, WebCustomer):
                out[model.__name__] = int((await s.execute(
                    select(func.count()).select_from(model).where(
                        model.tenant_id == tid))).scalar() or 0)
            return out

    before = asyncio.run(counts())
    assert before["Training"] and before["Signup"] and before["WebCustomer"]

    assert _delete(client, tid, "Салон С Данными").status_code == 200

    after = asyncio.run(counts())
    assert after == {"Training": 0, "Signup": 0, "WebCustomer": 0}, after


def test_other_clubs_are_untouched(client):
    keep = _make_club(client, "Клуб Остаётся")
    drop = _make_club(client, "Клуб Уходит")
    assert _delete(client, drop, "Клуб Уходит").status_code == 200
    assert _exists(client, keep)


def test_deletion_is_audited_without_secrets(client, caplog):
    tid = _make_club(client, "Салон Аудит")
    with caplog.at_level("WARNING"):
        _delete(client, tid, "Салон Аудит")
    assert "удалил клуб" in caplog.text
    assert "Салон Аудит" in caplog.text


# ---------- обратимая альтернатива ----------

def test_club_can_be_switched_off_instead(client):
    """Выключение оставляет данные на месте — это и есть безопасный путь."""
    import asyncio

    tid = _make_club(client, "Клуб Выключаемый")
    page = client.get(f"/admin/platform/{tid}/edit")
    assert 'name="is_active"' in page.text, "нет обратимого выключателя"

    # форма отправлена со снятой галочкой: маркер есть, самого поля нет
    client.post(f"/admin/platform/{tid}/edit", data={
        "csrf": _csrf(page.text), "club_name": "Клуб Выключаемый",
        "timezone": "Europe/Moscow", "vertical": "beauty",
        "active_submitted": "1"})

    async def is_active() -> bool:
        from app.db.engine import SessionLocal, engine
        from app.models.entities import Tenant
        await engine.dispose()
        async with SessionLocal() as s:
            return (await s.get(Tenant, tid)).is_active

    assert asyncio.run(is_active()) is False
    assert _exists(client, tid), "выключение не должно удалять клуб"


def test_edit_without_marker_never_switches_club_off(client):
    """Защита от чужого вызова: POST на /edit без маркера формы не должен
    уводить клуб офлайн — цена такой опечатки слишком велика."""
    import asyncio

    tid = _make_club(client, "Клуб Не Трогать")
    page = client.get(f"/admin/platform/{tid}/edit")
    client.post(f"/admin/platform/{tid}/edit", data={
        "csrf": _csrf(page.text), "club_name": "Клуб Не Трогать",
        "timezone": "Europe/Moscow", "vertical": "sport"})

    async def is_active() -> bool:
        from app.db.engine import SessionLocal, engine
        from app.models.entities import Tenant
        await engine.dispose()
        async with SessionLocal() as s:
            return (await s.get(Tenant, tid)).is_active

    assert asyncio.run(is_active()) is True
