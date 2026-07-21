"""
Персональная ссылка управления записями.

Раньше единственным способом увидеть свои записи был ввод телефона — то
есть телефон работал как пароль: зная чужой номер, можно было получить
чужой список и ссылки отмены. Ссылка управления случайна, в базе хранится
только её SHA-256, и она отзывается при удалении данных.
"""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

H = {"x-admin-token": "tok"}


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    from app.api import routes as api_routes
    api_routes._ip_hits.clear()
    yield
    api_routes._ip_hits.clear()


def _signup(c, phone="79130004455", name="Марина"):
    tid = c.post("/api/tenants", json={"name": "Клуб Ссылок"},
                 headers=H).json()["id"]
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    tr = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": "Тренировка", "start_at": start, "max_participants": 5,
    }).json()["id"]
    r = c.post(f"/club/{tid}/signup", data={
        "consent": "1", "training_id": tr, "name": name, "phone": phone})
    link = re.search(r'href="(/club/\d+/m/[\w-]+)"', r.text).group(1)
    return tid, tr, link


def test_signup_gives_manage_link_showing_own_bookings():
    with TestClient(app) as c:
        tid, _tr, link = _signup(c)
        page = c.get(link)
        assert page.status_code == 200
        assert "Тренировка" in page.text
        assert "Удалить мои данные" in page.text


def test_token_is_not_stored_in_plain_text():
    """Утечка дампа не должна давать доступ к чужим записям."""
    import asyncio

    with TestClient(app) as c:
        _tid, _tr, link = _signup(c, phone="79130009911")
        token = link.rsplit("/", 1)[1]

        async def stored():
            from sqlalchemy import select

            from app.db.engine import SessionLocal, engine
            from app.models.entities import ManageToken
            await engine.dispose()
            async with SessionLocal() as s:
                rows = (await s.execute(select(ManageToken))).scalars().all()
                return [r.token_hash for r in rows]

        hashes = asyncio.run(stored())
        assert hashes, "токен не сохранён"
        assert token not in hashes
        assert all(len(h) == 64 for h in hashes)


def test_unknown_or_revoked_token_gives_404():
    with TestClient(app) as c:
        tid, _tr, link = _signup(c, phone="79130007788")
        assert c.get(f"/club/{tid}/m/явно-не-тот-токен").status_code == 404

        c.post(f"{link}/forget")
        assert c.get(link).status_code == 404, "ссылка не отозвана"


def test_cancel_from_manage_page():
    with TestClient(app) as c:
        tid, tr, link = _signup(c, phone="79130001212")
        r = c.post(f"{link}/cancel", data={"training_id": tr})
        assert r.status_code == 200
        assert "Запись отменена" in r.text

        signups = c.get(f"/api/tenants/{tid}/trainings/{tr}/signups",
                        headers=H).json()
        assert not [s for s in signups if s["status"] == "active"]


def test_export_returns_own_data():
    with TestClient(app) as c:
        _tid, _tr, link = _signup(c, phone="79130003434", name="Ольга")
        r = c.get(f"{link}/export")
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        data = r.json()
        assert data["записи"][0]["занятие"] == "Тренировка"
        assert data["телефон"] == "79130003434"   # свой номер видеть можно


def test_forget_removes_personal_data():
    with TestClient(app) as c:
        tid, tr, link = _signup(c, phone="79130005656", name="Пётр")
        r = c.post(f"{link}/forget")
        assert r.status_code == 200
        assert "Данные удалены" in r.text

        # запись исчезла у тренера, телефон в подписи тоже
        signups = c.get(f"/api/tenants/{tid}/trainings/{tr}/signups",
                        headers=H).json()
        assert signups == []
        by_phone = c.post(f"/club/{tid}/my", data={"phone": "79130005656"})
        assert "не найдено" in by_phone.text
