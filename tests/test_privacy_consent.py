"""Согласие на обработку данных: страница /privacy и галочка на формах.

Проверка обязательна на СЕРВЕРЕ: атрибут required в браузере отключается,
а форму можно отправить и минуя страницу.
"""
import datetime as dt

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


def _club_with_slot(c: TestClient) -> tuple[int, int]:
    tid = c.post("/api/tenants", json={"name": "Клуб Согласие"},
                 headers=H).json()["id"]
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    tr = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": "Занятие", "start_at": start, "max_participants": 8,
    }).json()["id"]
    return tid, tr


# ---------- страница ----------

def test_privacy_page_opens_and_covers_real_flows():
    with TestClient(app) as c:
        r = c.get("/privacy")
        assert r.status_code == 200
        html = r.text
        # то, что система действительно делает, должно быть названо
        assert "резервная копия" in html.lower()
        assert "Telegram" in html
        assert "IP-адрес" in html
        assert "Railway" in html


def test_privacy_linked_from_public_pages():
    with TestClient(app) as c:
        for path in ("/promo", "/faq", "/reviews"):
            assert '/privacy' in c.get(path).text, path
        tid, _ = _club_with_slot(c)
        assert '/privacy' in c.get(f"/club/{tid}").text


# ---------- запись на занятие ----------

def test_signup_form_has_consent_checkbox():
    with TestClient(app) as c:
        tid, _ = _club_with_slot(c)
        html = c.get(f"/club/{tid}").text
        assert 'name="consent"' in html
        assert 'Согласен на обработку имени и телефона' in html


def test_signup_rejected_without_consent():
    with TestClient(app) as c:
        tid, tr = _club_with_slot(c)
        r = c.post(f"/club/{tid}/signup",
                   data={"training_id": tr, "name": "Пётр",
                         "phone": "79110001122"})
        assert r.status_code == 400
        assert "согласие" in r.text.lower()

        ok = c.post(f"/club/{tid}/signup",
                    data={"training_id": tr, "name": "Пётр",
                          "phone": "79110001122", "consent": "1"})
        assert ok.status_code == 200
        assert "записаны" in ok.text.lower()


# ---------- оценка мастера ----------

def test_rate_master_rejected_without_consent():
    with TestClient(app) as c:
        tid, _ = _club_with_slot(c)
        mid = c.post(f"/api/tenants/{tid}/masters", headers=H,
                     json={"name": "Наталья"}).json()["id"]
        r = c.post(f"/club/{tid}/rate",
                   data={"master_id": mid, "rating": 5, "name": "Гость",
                         "phone": "79110002233"})
        assert r.status_code == 400


# ---------- отзыв о платформе ----------

def test_site_review_rejected_without_consent():
    with TestClient(app) as c:
        r = c.post("/reviews", data={"name": "Марина", "rating": 5,
                                     "text": "Всё удобно"})
        # форма возвращается с понятным сообщением, отзыв не сохранён
        assert r.status_code == 200
        assert "согласие" in r.text.lower()

        from app.api.public_style import CONSENT_ERROR
        assert CONSENT_ERROR in r.text
