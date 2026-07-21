"""
Восстановление доступа по одному номеру телефона запрещено.

Номер не секрет: его знают администратор, коллеги, любой, кто видел
запись, и он перебирается. Раньше ввод чужого номера показывал чужие
записи, ссылки их отмены и выдавал новую персональную ссылку управления.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import app

H = {"x-admin-token": "tok"}
PHONE = "79151112233"


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    from app.api import routes as api_routes
    api_routes._ip_hits.clear()
    yield
    api_routes._ip_hits.clear()


def _club_with_booking(c):
    tid = c.post("/api/tenants", json={"name": "Клуб Перебора"},
                 headers=H).json()["id"]
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    tr = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": "Секретное занятие", "start_at": start,
        "max_participants": 5,
    }).json()["id"]
    c.post(f"/club/{tid}/signup", data={
        "consent": "1", "training_id": tr, "name": "Виктор", "phone": PHONE})
    return tid, tr


def test_known_phone_gives_no_bookings_no_links_no_token():
    with TestClient(app) as c:
        tid, _tr = _club_with_booking(c)
        r = c.post(f"/club/{tid}/my", data={"phone": PHONE})

        assert r.status_code == 200
        # ни записей, ни ссылок отмены, ни новой персональной ссылки
        assert "Секретное занятие" not in r.text
        assert "/cancel?" not in r.text
        assert f"/club/{tid}/m/" not in r.text


def test_existing_and_unknown_phone_are_indistinguishable():
    """Иначе по ответу можно перебором узнать, кто ходит в этот клуб."""
    with TestClient(app) as c:
        tid, _tr = _club_with_booking(c)

        known = c.post(f"/club/{tid}/my", data={"phone": PHONE})
        unknown = c.post(f"/club/{tid}/my", data={"phone": "79150000000"})
        garbage = c.post(f"/club/{tid}/my", data={"phone": "не телефон"})
        empty = c.post(f"/club/{tid}/my", data={"phone": ""})

        codes = {known.status_code, unknown.status_code,
                 garbage.status_code, empty.status_code}
        assert codes == {200}, codes
        assert known.text == unknown.text == garbage.text == empty.text


def test_no_manage_token_is_issued_by_phone():
    """Регресс: обращение с номером не должно плодить токены доступа."""
    import asyncio

    with TestClient(app) as c:
        tid, _tr = _club_with_booking(c)

        async def token_count():
            from sqlalchemy import select

            from app.db.engine import SessionLocal, engine
            from app.models.entities import ManageToken
            await engine.dispose()
            async with SessionLocal() as s:
                rows = (await s.execute(select(ManageToken).where(
                    ManageToken.tenant_id == tid))).scalars().all()
                return len(rows)

        before = asyncio.run(token_count())
        for _ in range(3):
            c.post(f"/club/{tid}/my", data={"phone": PHONE})
        assert asyncio.run(token_count()) == before


def test_club_page_has_no_phone_lookup_form():
    with TestClient(app) as c:
        tid, _tr = _club_with_booking(c)
        page = c.get(f"/club/{tid}").text
        assert f'action="/club/{tid}/my"' not in page
        assert "Показать мои записи" not in page
        # вместо формы — объяснение, где взять личную ссылку
        assert "личной ссылке" in page or "личную ссылку" in page


def test_my_help_page_explains_without_lookup():
    with TestClient(app) as c:
        tid, _tr = _club_with_booking(c)
        r = c.get(f"/club/{tid}/my-help")
        assert r.status_code == 200
        assert "Секретное занятие" not in r.text
        assert "администратору клуба" in r.text
