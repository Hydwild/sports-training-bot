"""
Персональная ссылка управления записями и сессия по ней.

Раньше единственным способом увидеть свои записи был ввод телефона — то
есть телефон работал как пароль: зная чужой номер, можно было получить
чужой список и ссылки отмены. Ссылка управления случайна, в базе хранится
только её SHA-256, а дальше человек работает по адресу без токена: токен
в URL остаётся в истории браузера, в Referer и в логах прокси.
"""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

H = {"x-admin-token": "tok"}


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    from app.api import rate_limit
    rate_limit._memory.clear()
    yield
    rate_limit._memory.clear()


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
        page = c.get(link)                      # редирект на чистый адрес
        assert page.status_code == 200
        assert str(page.url).endswith(f"/club/{tid}/manage")
        assert "Тренировка" in page.text
        assert "Удалить мои данные" in page.text


def test_token_exchanged_for_cookie_and_url_cleaned():
    with TestClient(app) as c:
        tid, _tr, link = _signup(c, phone="79130002020")
        token = link.rsplit("/", 1)[1]

        r = c.get(link, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/club/{tid}/manage"
        # токена в адресе больше нет — он не попадёт в историю и Referer
        assert token not in r.headers["location"]

        cookie = r.headers["set-cookie"]
        assert f"manage_{tid}=" in cookie
        assert "HttpOnly" in cookie                  # недоступна из JS
        assert "samesite=lax" in cookie.lower()      # не уходит с чужих сайтов
        assert f"Path=/club/{tid}" in cookie         # не уходит в другой клуб
        # В cookie — секрет КОРОТКОЙ сессии, а НЕ долгоживущий токен ссылки:
        # утечка cookie не даёт токен из адреса, и наоборот.
        import re as _re
        cookie_val = _re.search(rf"manage_{tid}=([^;]+)", cookie).group(1)
        assert cookie_val != token
        # страница с персональными данными не должна оседать в кешах
        page = c.get(f"/club/{tid}/manage")
        assert "no-store" in page.headers["cache-control"]
        assert page.headers["referrer-policy"] == "no-referrer"


def test_link_is_single_use():
    """Пересланная или подсмотренная ссылка после первого визита
    бесполезна: второй обмен уже не срабатывает."""
    with TestClient(app) as c:
        tid, _tr, link = _signup(c, phone="79130002121")
        first = c.get(link)
        assert first.status_code == 200        # обменялась на сессию

        c.cookies.clear()                      # как будто это другой браузер
        second = c.get(link)
        assert second.status_code == 404, "ссылка сработала повторно"


def test_used_link_kills_nothing_of_active_session():
    """После обмена активная сессия работает, даже что ссылка уже
    использована."""
    with TestClient(app) as c:
        tid, _tr, link = _signup(c, phone="79130002323")
        c.get(link)                            # сессия установлена
        # ссылка использована, но сессия жива
        assert c.get(f"/club/{tid}/manage").status_code == 200


def test_manage_pages_need_session():
    with TestClient(app) as c:
        tid, tr, _link = _signup(c, phone="79130003030")
        c.cookies.clear()
        assert c.get(f"/club/{tid}/manage").status_code == 404
        assert c.get(f"/club/{tid}/manage/export").status_code == 404
        assert c.post(f"/club/{tid}/manage/cancel",
                      data={"training_id": tr}).status_code == 404
        assert c.post(f"/club/{tid}/manage/forget").status_code == 404


def test_session_does_not_work_in_another_club():
    """Cookie одного клуба не открывает данные в другом."""
    with TestClient(app) as c:
        tid_a, _tr, link_a = _signup(c, phone="79130004040")
        c.get(link_a)                                # сессия клуба A
        tid_b, _tr_b, _link_b = _signup(c, phone="79130005050")
        # у клуба B своя cookie; подменяем её значением из A
        c.cookies.set(f"manage_{tid_b}", c.cookies.get(f"manage_{tid_a}"))
        assert c.get(f"/club/{tid_b}/manage").status_code == 404


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


def test_unknown_token_gives_404():
    with TestClient(app) as c:
        tid, _tr, _link = _signup(c, phone="79130007788")
        assert c.get(f"/club/{tid}/m/явно-не-тот-токен").status_code == 404


def test_cancel_from_manage_page():
    with TestClient(app) as c:
        tid, tr, link = _signup(c, phone="79130001212")
        c.get(link)
        r = c.post(f"/club/{tid}/manage/cancel", data={"training_id": tr})
        assert r.status_code == 200
        assert "Запись отменена" in r.text

        signups = c.get(f"/api/tenants/{tid}/trainings/{tr}/signups",
                        headers=H).json()
        assert not [s for s in signups if s["status"] == "active"]


def test_export_returns_own_data():
    with TestClient(app) as c:
        tid, _tr, link = _signup(c, phone="79130003434", name="Ольга")
        c.get(link)
        r = c.get(f"/club/{tid}/manage/export")
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        assert "no-store" in r.headers["cache-control"]
        data = r.json()
        assert data["записи"][0]["занятие"] == "Тренировка"
        assert data["телефон"] == "79130003434"   # свой номер видеть можно


def test_forget_removes_personal_data_and_revokes_access():
    import asyncio

    with TestClient(app) as c:
        tid, tr, link = _signup(c, phone="79130005656", name="Пётр")
        c.get(link)
        r = c.post(f"/club/{tid}/manage/forget")
        assert r.status_code == 200
        assert "Данные удалены" in r.text

        # запись исчезла у тренера
        signups = c.get(f"/api/tenants/{tid}/trainings/{tr}/signups",
                        headers=H).json()
        assert signups == []

        # клиент с зашифрованным телефоном удалён
        async def customers_left():
            from sqlalchemy import select

            from app.db.engine import SessionLocal, engine
            from app.models.entities import WebCustomer
            await engine.dispose()
            async with SessionLocal() as s:
                rows = (await s.execute(select(WebCustomer).where(
                    WebCustomer.tenant_id == tid))).scalars().all()
                return len(rows)

        assert asyncio.run(customers_left()) == 0
        # и старая ссылка больше не работает
        assert c.get(link).status_code == 404


def test_new_link_revokes_the_previous_one():
    """Иначе у клиента копятся вечные ключи к своим данным: потерянная
    год назад ссылка открывала бы их до сих пор."""
    with TestClient(app) as c:
        tid, tr, first = _signup(c, phone="79130006060", name="Раиса")
        c.cookies.clear()

        start = (dt.datetime.now(dt.timezone.utc)
                 + dt.timedelta(days=3)).isoformat()
        tr2 = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
            "title": "Вторая", "start_at": start, "max_participants": 5,
        }).json()["id"]
        r = c.post(f"/club/{tid}/signup", data={
            "consent": "1", "training_id": tr2, "name": "Раиса",
            "phone": "79130006060"})
        second = re.search(r'href="(/club/\d+/m/[\w-]+)"', r.text).group(1)
        assert second != first

        c.cookies.clear()
        assert c.get(first).status_code == 404, "старая ссылка не отозвана"
        c.cookies.clear()
        assert c.get(second).status_code == 200
        assert tr


def test_expired_and_revoked_tokens_are_purged():
    import asyncio

    with TestClient(app) as c:
        tid, _tr, link = _signup(c, phone="79130007070")
        c.get(link)
        c.post(f"/club/{tid}/manage/forget")       # отзывает ссылку

        async def purge_and_count():
            from sqlalchemy import select

            from app.db.engine import SessionLocal, engine
            from app.models.entities import ManageToken
            from app.repositories.repo import TenantRepository
            await engine.dispose()
            async with SessionLocal() as s:
                removed = await TenantRepository(s, tid).purge_expired_manage_tokens()
                await s.commit()
                left = (await s.execute(select(ManageToken).where(
                    ManageToken.tenant_id == tid))).scalars().all()
                return removed, len(left)

        removed, left = asyncio.run(purge_and_count())
        assert removed >= 1
        assert left == 0
