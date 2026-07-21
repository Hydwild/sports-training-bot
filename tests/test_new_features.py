"""Тесты новых функций: расписания, веб-запись, конфигуратор, бэкап."""
import asyncio
import datetime as dt
import io
import re
import zipfile

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


def _mk_training(c, tid, title="Игра", days=10, maxp=2):
    start = (dt.datetime.now(dt.timezone.utc)
             + dt.timedelta(days=days)).isoformat()
    r = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": title, "start_at": start, "location": "Зал",
        "max_participants": maxp, "duration_min": 90})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_public_web_flow():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Веб-клуб"},
                     headers=H).json()["id"]
        tr = _mk_training(c, tid)
        # страница
        r = c.get(f"/club/{tid}")
        assert r.status_code == 200 and "Игра" in r.text
        # заголовок без эмодзи ракетки
        assert "<h1>Веб-клуб</h1>" in r.text
        assert "🏸" not in r.text
        # запись
        r = c.post(f"/club/{tid}/signup", data={"consent": "1", 
            "training_id": tr, "name": "Олег", "phone": "79123456789"})
        assert "Вы записаны" in r.text and "/cancel?" in r.text
        # ссылку отмены человек получает сразу при записи — сохраняем её
        cancel_link = re.search(
            r'href="(/club/\d+/cancel\?[^"]+)"', r.text).group(1)
        # имена записанных видны на странице
        assert "Олег" in c.get(f"/club/{tid}").text
        # повтор
        r = c.post(f"/club/{tid}/signup", data={"consent": "1", 
            "training_id": tr, "name": "Олег", "phone": "79123456789"})
        assert "уже записаны" in r.text
        # по номеру телефона записи больше не показываются: номер знают и
        # другие люди, восстановление доступа по нему закрыто
        r_my = c.post(f"/club/{tid}/my", data={"phone": "+7 912 345-67-89"})
        assert r_my.status_code == 200
        assert "Игра" not in r_my.text and "/cancel?" not in r_my.text
        # отмена по персональной ссылке, полученной при записи
        link = cancel_link.replace("&amp;", "&")
        # переход по ссылке ничего не отменяет — только спрашивает
        confirm = c.get(link)
        assert "Отменить запись?" in confirm.text
        assert "отменена" not in confirm.text
        # отменяет только подтверждение формой
        fields = dict(re.findall(r'name="(\w+)" value="([^"]+)"', confirm.text))
        done = c.post(link.split("?")[0], data=fields)
        assert "отменена" in done.text
        # подделанный токен отклоняется
        assert c.get(re.sub(r"s=\w+", "s=deadbeef", link)).status_code == 403
        assert c.post(link.split("?")[0],
                      data={**fields, "s": "deadbeef"}).status_code == 403
        # валидация телефона
        r = c.post(f"/club/{tid}/signup", data={"consent": "1", 
            "training_id": tr, "name": "X", "phone": "12"})
        assert r.status_code == 400
        # QR
        r = c.get(f"/club/{tid}/qr")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"


def test_public_rate_limit():
    from app.api import rate_limit
    rate_limit._memory.clear()
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Спам-клуб"},
                     headers=H).json()["id"]
        tr = _mk_training(c, tid, maxp=50)
        codes = []
        for i in range(7):
            r = c.post(f"/club/{tid}/signup", data={"consent": "1", 
                "training_id": tr, "name": f"U{i}",
                "phone": f"7912000{i:04d}"})
            codes.append(r.status_code)
        assert codes.count(429) >= 1, codes  # лишние запросы отбиты
    rate_limit._memory.clear()


def test_schedule_autocreate_and_dedup():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Расписание-клуб"},
                     headers=H).json()["id"]

    async def run():
        from app.db.engine import SessionLocal, engine
        from app.services.booking import BookingService
        from app.services import tasks
        await engine.dispose()   # сбрасываем пул из чужого event loop
        async with SessionLocal() as s:
            svc = BookingService(s, tid)
            wd = (dt.datetime.now(dt.timezone.utc).weekday() + 1) % 7
            await svc.repo.add_schedule(
                weekday=wd, time_str="19:00", title="Регулярка",
                location="Зал", duration_min=60, price_minor=0,
                max_participants=6, days_ahead=3)
            await s.commit()
        await tasks._process_schedules()
        await tasks._process_schedules()   # второй прогон — дублей нет
        async with SessionLocal() as s:
            svc = BookingService(s, tid)
            trs = [t for t in await svc.repo.list_upcoming()
                   if t.title == "Регулярка"]
            assert len(trs) == 1, f"дубли или не создано: {len(trs)}"

    asyncio.run(run())


def test_builder_and_backup():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб-Б"},
                     headers=H).json()["id"]
        c.post(f"/api/tenants/{tid}/members", headers=H,
               json={"tg_user_id": 901, "role": "owner", "name": "O"})
        c.post("/admin/auth/dev", data={"tg_user_id": 901})
        # конфигуратор (csrf берём со страницы, как браузер)
        page = c.get("/admin/builder")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        r = c.post("/admin/builder", data={
            "csrf": csrf,
            "club_name": "Client", "edition": "lite",
            "timezone": "Europe/Moscow", "tg_token": "1:X", "vk_token": "",
            "admin_tg_id": "42", "welcome_text": "Привет!",
            "reminder_minutes": "60", "cancel_lock_minutes": "0",
            "brand_color": "#3a7bd5"})
        assert r.status_code == 200
        # без csrf — отказ
        assert c.post("/admin/builder", data={
            "club_name": "X", "tg_token": "1:X"}).status_code == 403
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        assert "app/main.py" in names and ".env" in names
        assert "seed.db" in names
        assert "TG_TOKEN=1:X" in z.read(".env").decode()
        # бэкап
        r = c.get("/admin/backup")
        assert r.status_code == 200
        assert r.content[:15].startswith(b"SQLite format")
        # не-owner получает отказ
        c.post(f"/api/tenants/{tid}/members", headers=H,
               json={"tg_user_id": 902, "role": "coach", "name": "C"})
        c.post("/admin/auth/dev", data={"tg_user_id": 902})
        assert c.get("/admin/builder").status_code in (401, 403)


def test_monthly_summary_and_past():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Архив-клуб"},
                     headers=H).json()["id"]

    async def run():
        from app.db.engine import SessionLocal, engine
        from app.services.booking import BookingService
        await engine.dispose()   # сбрасываем пул из чужого event loop
        async with SessionLocal() as s:
            svc = BookingService(s, tid)
            tr = await svc.create_training(
                title="Прошлая",
                start_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5),
                location="З", max_participants=4, duration_min=60,
                state="published", publish_at=None, platform="tg", user_id=0)
            await svc.sign_up(tr.id, "tg", 7, "A")
            await s.commit()
            (await svc.repo.get_signups(tr.id, "active"))[0].attended = True
            await s.commit()
            past = await svc.repo.list_past()
            summ = await svc.monthly_summary()
            assert len(past) == 1
            assert summ and summ[0]["trainings"] == 1
            assert summ[0]["attended"] == 1

    asyncio.run(run())


def test_faq_page():
    with TestClient(app) as c:
        r = c.get("/faq")
        assert r.status_code == 200
        assert "Вопросы и ответы" in r.text
        assert "Как записаться?" in r.text


def test_promo_and_demo_seed():
    with TestClient(app) as c:
        r = c.get("/promo")
        assert r.status_code == 200 and "Боты" in r.text and "для записей" in r.text
        from app.api.promo_page import DEMO_BOT_URL
        assert DEMO_BOT_URL in r.text
        tid = c.post("/api/tenants", json={"name": "Демо"},
                     headers=H).json()["id"]

    async def run():
        from app.db.engine import SessionLocal
        from app.services.booking import BookingService
        async with SessionLocal() as s:
            svc = BookingService(s, tid)
            assert await svc.seed_demo() is True
            ups = await svc.repo.list_upcoming()
            assert len(ups) == 3
            assert await svc.repo.list_schedules()
            assert (await svc.monthly_summary())[0]["attended"] >= 1
            # повторно — отказ (клуб уже не пуст)
            assert await svc.seed_demo() is False

    asyncio.run(run())


def test_legacy_avatar_scrub():
    """Старт приложения вычищает legacy-аватары с токеном бота внутри URL."""
    from sqlalchemy import select
    from app.db.engine import SessionLocal
    from app.models.entities import Subscriber

    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Аватар-клуб"},
                     headers=H).json()["id"]

    leaky = "https://api.telegram.org/file/bot123:ABCDEF/photos/file_1.jpg"

    async def seed():
        async with SessionLocal() as s:
            s.add(Subscriber(tenant_id=tid, platform="tg", user_id=1,
                             name="U", photo_url=leaky))
            s.add(Subscriber(tenant_id=tid, platform="vk", user_id=2,
                             name="V", photo_url="https://vk.com/ok.jpg"))
            await s.commit()
    asyncio.run(seed())

    # повторный старт запускает очистку в lifespan
    with TestClient(app):
        pass

    async def check():
        async with SessionLocal() as s:
            rows = (await s.execute(select(Subscriber).where(
                Subscriber.tenant_id == tid))).scalars().all()
            by_uid = {r.user_id: r.photo_url for r in rows}
            assert by_uid[1] is None                         # токен вычищен
            assert by_uid[2] == "https://vk.com/ok.jpg"      # VK не тронут
    asyncio.run(check())
