"""
Администратор клуба может выдать новую ссылку управления клиенту, а обычная
запись/выпуск ссылки не гасит уже открытую cookie-сессию.
"""
import datetime as dt
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


def _login(c, tg_id, tid):
    c.post(f"/api/tenants/{tid}/members", headers=H,
           json={"tg_user_id": tg_id, "role": "owner", "name": "Владелец"})
    c.post("/admin/auth/dev", data={"tg_user_id": tg_id})


def _login_role(c, tg_id, tid, role):
    c.post(f"/api/tenants/{tid}/members", headers=H,
           json={"tg_user_id": tg_id, "role": role, "name": role})
    c.post("/admin/auth/dev", data={"tg_user_id": tg_id})


def _csrf(text):
    return re.search(r'name="csrf" value="([^"]+)"', text).group(1)


def _club_signup(c, phone="79220001111", name="Клиент"):
    tid = c.post("/api/tenants", json={"name": "Клуб Ссылок"},
                 headers=H).json()["id"]
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    tr = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": "Занятие", "start_at": start, "max_participants": 5,
    }).json()["id"]
    r = c.post(f"/club/{tid}/signup", data={
        "consent": "1", "training_id": tr, "name": name, "phone": phone})
    link = re.search(r'href="(/club/\d+/m/[\w-]+)"', r.text).group(1)
    # web-клиент виден админу в записях; достанем его uid из БД
    import asyncio

    async def uid():
        from sqlalchemy import select

        from app.db.engine import SessionLocal, engine
        from app.models.entities import WebCustomer
        await engine.dispose()
        async with SessionLocal() as s:
            return (await s.execute(select(WebCustomer.id).where(
                WebCustomer.tenant_id == tid))).scalar_one()

    return tid, tr, link, asyncio.run(uid())


def test_admin_can_issue_link_and_it_works_once():
    with TestClient(app) as c:
        tid, tr, _link, uid = _club_signup(c)
        _login(c, 7001, tid)
        page = c.get(f"/admin/trainings/{tr}")
        r = c.post("/admin/manage-link",
                   data={"csrf": _csrf(page.text), "web_user_id": uid})
        assert r.status_code == 200
        assert "no-store" in r.headers["cache-control"]
        new_link = re.search(r'value="(https?://[^"]+/club/\d+/m/[\w-]+)"',
                             r.text).group(1)

        c.cookies.clear()
        first = c.get(new_link)                 # обмен на сессию
        assert first.status_code == 200
        c.cookies.clear()
        assert c.get(new_link).status_code == 404   # одноразовая


def test_admin_of_other_tenant_cannot_issue():
    with TestClient(app) as c:
        tid_a, _tr_a, _l, uid_a = _club_signup(c, phone="79220002222")
        # админ другого клуба
        tid_b = c.post("/api/tenants", json={"name": "Чужой клуб"},
                       headers=H).json()["id"]
        _login(c, 7002, tid_b)
        page = c.get("/admin/masters")           # страница с csrf tenant B
        # пытается выдать ссылку клиенту клуба A
        r = c.post("/admin/manage-link",
                   data={"csrf": _csrf(page.text), "web_user_id": uid_a})
        assert r.status_code == 404          # tenant isolation


def test_unauthenticated_cannot_issue():
    with TestClient(app) as c:
        tid, _tr, _l, uid = _club_signup(c, phone="79220003333")
        c.cookies.clear()
        r = c.post("/admin/manage-link",
                   data={"csrf": "x", "web_user_id": uid},
                   follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403)


def test_csrf_required():
    with TestClient(app) as c:
        tid, tr, _l, uid = _club_signup(c, phone="79220004444")
        _login(c, 7003, tid)
        r = c.post("/admin/manage-link", data={"web_user_id": uid})
        assert r.status_code == 403


def test_new_booking_does_not_end_active_session():
    """Дефект 2.4: раньше _issue_manage_link при новой записи гасил сессию."""
    with TestClient(app) as c:
        tid, tr, link, _uid = _club_signup(c, phone="79220005555", name="Оля")
        c.get(link)                              # открыли сессию
        assert c.get(f"/club/{tid}/manage").status_code == 200

        # тот же человек записывается на ещё одно занятие
        start = (dt.datetime.now(dt.timezone.utc)
                 + dt.timedelta(days=3)).isoformat()
        tr2 = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
            "title": "Второе", "start_at": start, "max_participants": 5,
        }).json()["id"]
        c.post(f"/club/{tid}/signup", data={
            "consent": "1", "training_id": tr2, "name": "Оля",
            "phone": "79220005555"})

        # сессия ДОЛЖНА остаться живой
        assert c.get(f"/club/{tid}/manage").status_code == 200


def test_issued_link_is_not_logged_or_stored_plaintext(caplog):
    import asyncio

    with TestClient(app) as c:
        tid, tr, _l, uid = _club_signup(c, phone="79220006666")
        _login(c, 7004, tid)
        page = c.get(f"/admin/trainings/{tr}")
        with caplog.at_level("INFO"):
            r = c.post("/admin/manage-link",
                       data={"csrf": _csrf(page.text), "web_user_id": uid})
        new_link = re.search(r'value="https?://[^"]+/club/\d+/m/([\w-]+)"',
                             r.text)
        token = new_link.group(1)
        # токен не в логах
        assert token not in caplog.text
        # в БД — только SHA-256
        async def hashes():
            from sqlalchemy import select

            from app.db.engine import SessionLocal, engine
            from app.models.entities import ManageToken
            await engine.dispose()
            async with SessionLocal() as s:
                return [h for (h,) in (await s.execute(
                    select(ManageToken.token_hash))).all()]

        hs = asyncio.run(hashes())
        assert token not in hs
        assert all(len(h) == 64 for h in hs)


def test_issued_link_is_absolute_url():
    """Ссылку администратор копирует и отправляет клиенту — относительный
    '/club/...' вне браузера админки не открывается."""
    with TestClient(app) as c:
        tid, tr, _l, uid = _club_signup(c, phone="79220008888")
        _login(c, 7005, tid)
        page = c.get(f"/admin/trainings/{tr}")
        r = c.post("/admin/manage-link",
                   data={"csrf": _csrf(page.text), "web_user_id": uid})
        assert r.status_code == 200
        link = re.search(r'value="([^"]+/m/[\w-]+)"', r.text).group(1)
        assert link.startswith("http://testserver/club/")
        # относительной формы в ответе не остаётся
        assert 'value="/club/' not in r.text
        # и она действительно рабочая
        c.cookies.clear()
        assert c.get(link).status_code == 200


def test_link_base_comes_from_config_not_request_host():
    """База берётся из PUBLIC_BASE_URL, а не из заголовка Host, который
    задаёт клиент: иначе подделанный Host увёл бы ссылку на чужой домен."""
    with TestClient(app) as c:
        tid, tr, _l, uid = _club_signup(c, phone="79220013333")
        _login(c, 7010, tid)
        page = c.get(f"/admin/trainings/{tr}")
        r = c.post("/admin/manage-link",
                   data={"csrf": _csrf(page.text), "web_user_id": uid},
                   headers={"Host": "evil.example.com"})
        assert r.status_code == 200
        link = re.search(r'value="([^"]+/m/[\w-]+)"', r.text).group(1)
        assert "evil.example.com" not in link
        assert link.startswith("http://testserver/club/")


def test_no_public_base_url_fails_without_revoking_old_link(monkeypatch):
    """Без PUBLIC_BASE_URL абсолютную ссылку не построить. Тогда НЕ сохраняем
    ничего: прежняя ссылка клиента остаётся рабочей, а не оказывается
    отозванной ради ссылки, которую мы не смогли показать."""
    from app.core.config import settings as st

    with TestClient(app) as c:
        tid, tr, link, uid = _club_signup(c, phone="79220009999")
        _login(c, 7006, tid)
        page = c.get(f"/admin/trainings/{tr}")
        monkeypatch.setattr(st, "public_base_url", "")
        r = c.post("/admin/manage-link",
                   data={"csrf": _csrf(page.text), "web_user_id": uid})
        assert r.status_code == 500
        assert "PUBLIC_BASE_URL" in r.text
        monkeypatch.undo()
        # прежняя (неиспользованная) ссылка клиента не отозвана
        c.cookies.clear()
        assert c.get(link).status_code == 200


def test_assistant_does_not_see_manage_link_button():
    """Кнопка — ровно под ту же роль, что требует эндпойнт (coach). Иначе
    ассистент видит кнопку, жмёт и получает 403."""
    with TestClient(app) as c:
        tid, tr, _l, _uid = _club_signup(c, phone="79220010000")
        _login_role(c, 7007, tid, "assistant")
        page = c.get(f"/admin/trainings/{tr}")
        assert page.status_code == 200
        assert "/admin/manage-link" not in page.text
        # прочие действия ассистента на месте — скрыта именно эта кнопка
        assert "toggle_attend" in page.text


def test_assistant_cannot_issue_link():
    with TestClient(app) as c:
        tid, tr, _l, uid = _club_signup(c, phone="79220011111")
        _login_role(c, 7008, tid, "assistant")
        page = c.get(f"/admin/trainings/{tr}")
        r = c.post("/admin/manage-link",
                   data={"csrf": _csrf(page.text), "web_user_id": uid})
        assert r.status_code == 403


def test_coach_sees_manage_link_button():
    with TestClient(app) as c:
        tid, tr, _l, _uid = _club_signup(c, phone="79220012222")
        _login_role(c, 7009, tid, "coach")
        page = c.get(f"/admin/trainings/{tr}")
        assert "/admin/manage-link" in page.text


def test_forget_ends_links_and_sessions():
    import asyncio

    with TestClient(app) as c:
        tid, tr, link, uid = _club_signup(c, phone="79220007777")
        c.get(link)                              # активная сессия
        c.post(f"/club/{tid}/manage/forget")     # удаление данных

        async def counts():
            from sqlalchemy import func, select

            from app.db.engine import SessionLocal, engine
            from app.models.entities import ManageSession
            await engine.dispose()
            async with SessionLocal() as s:
                sess = (await s.execute(select(func.count()).select_from(
                    ManageSession).where(
                    ManageSession.tenant_id == tid))).scalar()
                return sess

        assert asyncio.run(counts()) == 0        # сессии сняты
        c.cookies.clear()
        assert c.get(link).status_code == 404    # ссылка мертва
