"""
Витрина клуба на публичной странице записи (/club/{id}): обложка, описание,
адрес/телефон, лента мастеров; редактирование через панель оператора.
"""
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


def _op_login(c):
    login = c.post("/admin/platform/login", data={"token": "tok"},
                   follow_redirects=False)
    c.cookies.set("platform_token", login.cookies["platform_token"])


def _edit_form(c, tid, **fields):
    page = c.get(f"/admin/platform/{tid}/edit")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    data = {"csrf": csrf, "club_name": fields.pop("club_name", "Клуб Витрины"),
            "timezone": "Europe/Moscow"}
    data.update(fields)
    return c.post(f"/admin/platform/{tid}/edit", data=data)


def test_profile_fields_saved_and_shown_on_club_page():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Клуб Витрины", "vertical": "beauty"},
            headers=H).json()["id"]
        _op_login(c)
        r = _edit_form(c, tid,
                       cover_url="https://example.com/cover.jpg",
                       about="Барбершоп в центре: стрижки и бритьё.",
                       address="ул. Ленина, 10",
                       contact_phone="+7 900 000-00-00")
        assert r.status_code == 200, r.text

        page = c.get(f"/club/{tid}").text
        assert 'src="https://example.com/cover.jpg"' in page
        assert "Барбершоп в центре" in page
        assert "ул. Ленина, 10" in page
        assert "+7 900 000-00-00" in page
        assert 'href="tel:+79000000000"' in page


def test_profile_absent_when_fields_empty():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Пустой Профиль"},
                     headers=H).json()["id"]
        page = c.get(f"/club/{tid}").text
        assert 'class="cover"' not in page
        assert 'class="about"' not in page
        assert 'class="biz-info"' not in page


def test_cover_url_must_be_http():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб XSS"},
                     headers=H).json()["id"]
        _op_login(c)
        r = _edit_form(c, tid, club_name="Клуб XSS",
                       cover_url="javascript:alert(1)")
        assert r.status_code == 400
        assert "http(s)-ссылкой" in r.text
        assert "javascript:alert" not in c.get(f"/club/{tid}").text


def _mk_training(c, tid, title="Слот", days=2, maxp=1, **extra):
    import datetime as dt
    start = (dt.datetime.now(dt.timezone.utc)
             + dt.timedelta(days=days)).isoformat()
    r = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": title, "start_at": start, "max_participants": maxp, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _visited(c, tid, master_id, phone, name="Клиент"):
    """Состоявшийся визит: человек записался к мастеру, и занятие прошло.

    Оценку принимаем только после визита, поэтому тестам рейтинга нужна
    настоящая история: записываемся на будущий слот через публичную форму,
    затем сдвигаем занятие в прошлое — как если бы оно уже состоялось."""
    import asyncio
    import datetime as dt

    tr = _mk_training(c, tid, title="Визит", maxp=50, master_id=master_id)
    r = c.post(f"/club/{tid}/signup", data={
        "consent": "1", "training_id": tr, "name": name, "phone": phone})
    assert r.status_code == 200, r.text

    async def move_to_past():
        from sqlalchemy import select

        from app.db.engine import SessionLocal, engine
        from app.models.entities import Training
        await engine.dispose()
        async with SessionLocal() as s:
            t = (await s.execute(
                select(Training).where(Training.id == tr))).scalar_one()
            t.start_at = (dt.datetime.now(dt.timezone.utc)
                          - dt.timedelta(hours=3))
            await s.commit()

    asyncio.run(move_to_past())
    return tr


def test_funnel_screens_present_with_masters():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Воронки", "vertical": "beauty"},
            headers=H).json()["id"]
        m = c.post(f"/api/tenants/{tid}/masters", headers=H,
                   json={"name": "Мастер Ника"}).json()
        tr = _mk_training(c, tid, title="Стрижка", master_id=m["id"])
        page = c.get(f"/club/{tid}").text
        # три экрана воронки
        assert 'id="scr-home"' in page
        assert 'id="scr-masters"' in page
        assert 'id="scr-slots"' in page
        # меню в стиле YClients
        assert "Выбрать мастера" in page
        assert "Выбрать дату и время" in page
        # чип ближайшего свободного окна ведёт на слот
        assert f'data-slot="{tr}"' in page
        assert f'data-m="{m["id"]}"' in page
        # карточка слота с атрибутом мастера и якорем
        assert f'data-master="{m["id"]}" id="slot-{tr}"' in page
        assert 'id="mfilter"' in page


def test_funnel_absent_without_masters():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб Без Воронки"},
                     headers=H).json()["id"]
        _mk_training(c, tid, title="Игра", maxp=5)
        page = c.get(f"/club/{tid}").text
        assert 'id="scr-home"' not in page      # прежний простой вид
        assert "Игра" in page


def test_funnel_full_slot_has_no_chip():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Занято", "vertical": "beauty"},
            headers=H).json()["id"]
        m = c.post(f"/api/tenants/{tid}/masters", headers=H,
                   json={"name": "Мастер Зоя"}).json()
        tr = _mk_training(c, tid, title="Занятый", master_id=m["id"])
        c.post(f"/club/{tid}/signup", data={"consent": "1", 
            "training_id": tr, "name": "Клиент", "phone": "79995556677"})
        page = c.get(f"/club/{tid}").text
        assert f'data-slot="{tr}"' not in page   # занятое окно не предлагаем
        assert "Свободных окон пока нет" in page


def test_master_bio_shown_on_masters_screen():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Био", "vertical": "beauty"}, headers=H).json()["id"]
        # через API
        m = c.post(f"/api/tenants/{tid}/masters", headers=H, json={
            "name": "Наталья", "specialty": "Парикмахер",
            "bio": "Опыт 3 года, колорист"}).json()
        assert m["bio"] == "Опыт 3 года, колорист"
        _mk_training(c, tid, title="Стрижка", master_id=m["id"])
        page = c.get(f"/club/{tid}").text
        assert "Наталья" in page and "Парикмахер" in page
        assert 'class="mbio">Опыт 3 года, колорист</p>' in page

        # через форму панели оператора
        _op_login(c)
        page2 = c.get(f"/admin/platform/{tid}/masters")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page2.text).group(1)
        r = c.post(f"/admin/platform/{tid}/masters/add", data={
            "csrf": csrf, "name": "Ирина", "specialty": "Бровист",
            "bio": "Опыт 5 лет"}, follow_redirects=False)
        assert r.status_code == 303
        page3 = c.get(f"/club/{tid}").text
        assert "Ирина" in page3 and "Опыт 5 лет" in page3


# ---------- рейтинг мастеров ----------

def test_rate_master_shows_average_and_count():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Рейтинга", "vertical": "beauty"},
            headers=H).json()["id"]
        m = c.post(f"/api/tenants/{tid}/masters", headers=H,
                   json={"name": "Мастер Рита"}).json()
        _mk_training(c, tid, title="Слот", master_id=m["id"])
        _visited(c, tid, m["id"], "79110000001", "Клиент А")
        _visited(c, tid, m["id"], "79110000002", "Клиент Б")

        r = c.post(f"/club/{tid}/rate", data={"consent": "1", 
            "master_id": m["id"], "rating": "5", "name": "Клиент А",
            "phone": "79110000001", "text": "Отличный мастер!"},
            follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/club/{tid}?rated=1"
        c.post(f"/club/{tid}/rate", data={"consent": "1", 
            "master_id": m["id"], "rating": "4", "name": "Клиент Б",
            "phone": "79110000002"})

        page = c.get(f"/club/{tid}?rated=1").text
        assert "★ 4.5" in page                    # среднее (5+4)/2
        assert "2 оценки" in page                 # плюрализация
        assert "Отличный мастер!" in page         # текст отзыва
        assert "Оценка сохранена" in page         # notice после редиректа
        assert "Оценить мастера" in page          # форма на карточке


def test_rate_master_same_phone_updates_not_duplicates():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Дублей", "vertical": "beauty"},
            headers=H).json()["id"]
        m = c.post(f"/api/tenants/{tid}/masters", headers=H,
                   json={"name": "Мастер Ева"}).json()
        _mk_training(c, tid, title="Слот", master_id=m["id"])
        _visited(c, tid, m["id"], "79110000009", "Тот же")

        c.post(f"/club/{tid}/rate", data={"consent": "1", 
            "master_id": m["id"], "rating": "2", "name": "Тот же",
            "phone": "79110000009"})
        c.post(f"/club/{tid}/rate", data={"consent": "1", 
            "master_id": m["id"], "rating": "5", "name": "Тот же",
            "phone": "79110000009"})
        page = c.get(f"/club/{tid}").text
        assert "★ 5.0" in page and "1 оценка" in page   # заменилась, не дубль


def test_rate_master_requires_real_visit():
    """Рейтинг — отзыв клиентов, а не опрос прохожих: без состоявшейся
    записи оценка не сохраняется, и человеку объясняют почему."""
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Визитов", "vertical": "beauty"},
            headers=H).json()["id"]
        m = c.post(f"/api/tenants/{tid}/masters", headers=H,
                   json={"name": "Мастер Инна"}).json()
        _mk_training(c, tid, title="Слот", master_id=m["id"])

        # чужой человек: записи нет
        r = c.post(f"/club/{tid}/rate", data={
            "consent": "1", "master_id": m["id"], "rating": "5",
            "name": "Прохожий", "phone": "79110007777"},
            follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/club/{tid}?rated=novisit"
        page = c.get(f"/club/{tid}?rated=novisit").text
        assert "после визита" in page
        assert "★" not in page.split('class="ms-strip"')[0]  # рейтинга нет

        # запись есть, но занятие ещё не прошло — тоже рано
        tr = _mk_training(c, tid, title="Будущее", maxp=50,
                          master_id=m["id"])
        c.post(f"/club/{tid}/signup", data={
            "consent": "1", "training_id": tr, "name": "Ранний",
            "phone": "79110008888"})
        early = c.post(f"/club/{tid}/rate", data={
            "consent": "1", "master_id": m["id"], "rating": "5",
            "name": "Ранний", "phone": "79110008888"},
            follow_redirects=False)
        assert early.headers["location"] == f"/club/{tid}?rated=novisit"

        # после визита — принимаем
        _visited(c, tid, m["id"], "79110009999", "Настоящий")
        ok = c.post(f"/club/{tid}/rate", data={
            "consent": "1", "master_id": m["id"], "rating": "5",
            "name": "Настоящий", "phone": "79110009999"},
            follow_redirects=False)
        assert ok.headers["location"] == f"/club/{tid}?rated=1"
        assert "★ 5.0" in c.get(f"/club/{tid}").text


def test_rate_master_validation_and_unknown_master():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Валид", "vertical": "beauty"},
            headers=H).json()["id"]
        m = c.post(f"/api/tenants/{tid}/masters", headers=H,
                   json={"name": "Мастер"}).json()
        # плохой рейтинг
        assert c.post(f"/club/{tid}/rate", data={"consent": "1", 
            "master_id": m["id"], "rating": "9", "name": "X Y",
            "phone": "79110000003"}).status_code == 400
        # плохой телефон
        assert c.post(f"/club/{tid}/rate", data={"consent": "1", 
            "master_id": m["id"], "rating": "5", "name": "X Y",
            "phone": "12"}).status_code == 400
        # несуществующий мастер
        assert c.post(f"/club/{tid}/rate", data={"consent": "1", 
            "master_id": 99999, "rating": "5", "name": "X Y",
            "phone": "79110000003"}).status_code == 404


def test_master_review_admin_delete():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Зачистки", "vertical": "beauty"},
            headers=H).json()["id"]
        m = c.post(f"/api/tenants/{tid}/masters", headers=H,
                   json={"name": "Мастер"}).json()
        _mk_training(c, tid, title="Слот", master_id=m["id"])
        _visited(c, tid, m["id"], "79110000004", "Спамер")
        c.post(f"/club/{tid}/rate", data={"consent": "1", 
            "master_id": m["id"], "rating": "1", "name": "Спамер",
            "phone": "79110000004", "text": "спам"})
        assert "★ 1.0" in c.get(f"/club/{tid}").text

        # id оценки достаём через повторный upsert недоступен — найдём в БД
        import asyncio

        async def get_rid():
            from app.db.engine import SessionLocal, engine
            from sqlalchemy import select
            from app.models.entities import MasterReview
            await engine.dispose()
            async with SessionLocal() as s:
                r = (await s.execute(select(MasterReview).where(
                    MasterReview.tenant_id == tid))).scalars().first()
                return r.id

        rid = asyncio.run(get_rid())
        # без токена нельзя
        assert c.delete(
            f"/api/tenants/{tid}/master-reviews/{rid}").status_code == 401
        assert c.delete(f"/api/tenants/{tid}/master-reviews/{rid}",
                        headers=H).json()["ok"] is True
        assert "★ 1.0" not in c.get(f"/club/{tid}").text


def test_masters_strip_shows_active_only():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Ленты", "vertical": "beauty"},
            headers=H).json()["id"]
        m1 = c.post(f"/api/tenants/{tid}/masters", headers=H, json={
            "name": "Активная Анна", "specialty": "Барбер"}).json()
        m2 = c.post(f"/api/tenants/{tid}/masters", headers=H, json={
            "name": "Скрытая Мария"}).json()
        c.delete(f"/api/tenants/{tid}/masters/{m2['id']}", headers=H)

        page = c.get(f"/club/{tid}").text
        assert 'class="ms-strip"' in page
        assert "Активная Анна" in page and "Барбер" in page
        assert "Скрытая Мария" not in page
        assert m1["id"] > 0
