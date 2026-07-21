"""
Регрессы по внешнему аудиту:
  1) лимит запросов за обратным прокси (Railway) — считался по IP прокси,
     то есть был ОБЩИМ на всех посетителей;
  2) день бэкапа помечался выполненным даже когда копия не ушла;
  3) свёрнутый ответ FAQ оставался доступен скринридеру и Tab.
"""
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


# ---------- 1. IP за прокси ----------

def test_client_ip_ignores_client_supplied_headers():
    """Регресс: раньше X-Forwarded-For разбирался в обработчике, и любой
    клиент присылал свой адрес сам — новая «личность» на каждый запрос,
    лимит обходился одной строкой. Доверие к заголовку теперь настраивается
    при запуске (uvicorn --forwarded-allow-ips), а не в коде."""
    from app.api.routes import client_ip

    class _Req:
        def __init__(self, headers, host="10.0.0.1"):
            self.headers = headers
            self.client = type("C", (), {"host": host})()

    assert client_ip(_Req({"x-forwarded-for": "203.0.113.7"})) == "10.0.0.1"
    assert client_ip(_Req({"x-real-ip": "203.0.113.9"})) == "10.0.0.1"
    assert client_ip(_Req({})) == "10.0.0.1"


def test_client_ip_normalizes_and_bounds_value():
    from app.api.routes import client_ip

    class _Req:
        def __init__(self, host):
            self.headers = {}
            self.client = type("C", (), {"host": host})()

    # IPv6 приводится к канонической записи
    assert client_ip(_Req("2001:0db8:0000:0000:0000:0000:0000:0001")) == "2001:db8::1"
    # мусор не роняет и не растёт бесконтрольно
    assert client_ip(_Req("x" * 500))[:10] == "xxxxxxxxxx"
    assert len(client_ip(_Req("x" * 500))) <= 45
    assert client_ip(_Req("")) == "?"


def test_spoofed_header_cannot_reset_the_limit():
    """Смена X-Forwarded-For больше не даёт новый лимит."""
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб Подделок"},
                     headers=H).json()["id"]
        import datetime as dt
        start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).isoformat()
        tr = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
            "title": "Игра", "start_at": start, "max_participants": 50,
        }).json()["id"]

        codes = []
        for i in range(8):
            r = c.post(f"/club/{tid}/signup",
                       # каждый раз новый «адрес» — раньше это сбрасывало счёт
                       headers={"x-forwarded-for": f"203.0.113.{i}"},
                       data={"consent": "1", "training_id": tr, "name": f"A{i}",
                             "phone": f"7911000{i:04d}"})
            codes.append(r.status_code)
        assert 429 in codes, codes


def test_limit_response_tells_when_to_retry():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб Retry"},
                     headers=H).json()["id"]
        import datetime as dt
        start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).isoformat()
        tr = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
            "title": "Игра", "start_at": start, "max_participants": 50,
        }).json()["id"]

        last = None
        for i in range(8):
            last = c.post(f"/club/{tid}/signup", data={
                "consent": "1", "training_id": tr, "name": f"B{i}",
                "phone": f"7911777{i:04d}"})
            if last.status_code == 429:
                break
        assert last.status_code == 429
        assert int(last.headers["retry-after"]) > 0


# ---------- 2. Бэкап ----------

async def test_backup_failure_does_not_mark_day_done(monkeypatch):
    """Если копия не ушла — день не закрывается, планировщик повторит."""
    from app.services import backup, tasks
    from app.services.backup import BackupResult

    async def fake_fail():
        return BackupResult(False, "Telegram недоступен")

    alerts = []

    async def fake_alert(where, err):
        alerts.append((where, str(err)))

    monkeypatch.setattr(backup, "send_backup_to_owner", fake_fail)
    monkeypatch.setattr(tasks, "_alert_admins", fake_alert)

    last_day = [None]
    await tasks._offsite_backup(last_day)
    assert last_day[0] is None, "день закрыт, хотя бэкапа нет"
    assert alerts and "бэкап" in alerts[0][0]


async def test_backup_success_marks_day_done(monkeypatch):
    import datetime as dt
    from app.services import backup, tasks
    from app.services.backup import BackupResult

    async def fake_ok():
        return BackupResult(True, "Бэкап отправлен")

    monkeypatch.setattr(backup, "send_backup_to_owner", fake_ok)
    last_day = [None]
    await tasks._offsite_backup(last_day)
    assert last_day[0] == dt.date.today().isoformat()


# ---------- 3. Доступность FAQ ----------

def test_faq_collapsed_answer_hidden_from_screen_readers():
    from app.api.faq_page import render_faq_page
    FAQ_HTML = render_faq_page()
    # свёрнутый блок скрыт visibility (убран из дерева доступности и Tab)
    assert "visibility:hidden" in FAQ_HTML
    assert "details.js-acc.expanded .faq-body{opacity:1;visibility:visible" in FAQ_HTML
    # скрипт больше не держит <details> принудительно открытым
    assert "d.open = false;" in FAQ_HTML
    assert "d.open = true;" in FAQ_HTML   # открывается только при раскрытии


# ---------- 4. Честность формулировок ----------

def test_promo_has_no_unsupported_claims():
    from app.api.promo_page import PROMO_HTML
    assert "сети филиалов" not in PROMO_HTML       # филиалов как сущности нет
    assert "Автотесты при обновлениях" not in PROMO_HTML


def test_faq_rating_claim_is_honest():
    from app.api.faq_page import render_faq_page
    FAQ_HTML = render_faq_page()
    assert "Защита от накрутки" not in FAQ_HTML
    # честно про телефон: он больше не подтверждает личность вообще
    assert "оценку больше не подтверждает" in FAQ_HTML
    # и про то, чего требуем взамен
    assert "отметка явки" in FAQ_HTML


def test_reviews_average_hidden_until_enough():
    from app.api.reviews_page import MIN_REVIEWS_FOR_AVG, render_reviews_page

    class _R:
        def __init__(self, rating, text="Отзыв"):
            self.rating = rating
            self.text = text
            self.name = "Клиент"
            self.club_name = ""

    one = render_reviews_page([_R(5)])
    assert "средняя оценка" not in one              # 5.0 по одному отзыву — нет
    assert "1</b><span>отзыв" in one

    many = render_reviews_page([_R(5)] * MIN_REVIEWS_FOR_AVG)
    assert "средняя оценка" in many
