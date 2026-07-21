"""
Публичная страница отзывов (/reviews) и её модерация в панели оператора
(/admin/platform/reviews): новый отзыв не виден на публичной странице, пока
оператор его не одобрит; honeypot и rate limit защищают форму от спама.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

TOKEN = "tok"  # см. tests/conftest.py: ADMIN_API_TOKEN=tok


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    from app.api import routes as api_routes
    api_routes._ip_hits.clear()
    yield
    api_routes._ip_hits.clear()


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def _login(c: TestClient) -> None:
    login = c.post("/admin/platform/login", data={"token": TOKEN},
                   follow_redirects=False)
    c.cookies.set("platform_token", login.cookies["platform_token"])


def test_reviews_page_empty_state():
    with TestClient(app) as c:
        r = c.get("/reviews")
        assert r.status_code == 200
        assert "Отзывов пока нет" in r.text


def test_submitted_review_pending_not_shown_publicly():
    with TestClient(app) as c:
        r = c.post("/reviews", data={"consent": "1", 
            "name": "Игорь", "club_name": "Смэш-клуб", "rating": "5",
            "text": "Отличный бот, участники сами записываются."},
            follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/reviews?sent=1"

        page = c.get("/reviews?sent=1")
        assert "Спасибо" in page.text
        # отзыв ещё не одобрен — на публичной странице его не видно
        assert "Игорь" not in page.text
        assert "Отличный бот" not in page.text


def test_honeypot_filled_silently_drops_without_creating_review():
    with TestClient(app) as c:
        r = c.post("/reviews", data={"consent": "1", 
            "name": "Бот", "rating": "5", "text": "спам-текст",
            "website": "http://spam.example"}, follow_redirects=False)
        assert r.status_code == 303  # тихо "принимаем", чтобы не подсказывать боту

        _login(c)
        mod = c.get("/admin/platform/reviews")
        assert "Бот" not in mod.text  # отзыв не создан вовсе


def test_empty_fields_rejected_with_notice():
    with TestClient(app) as c:
        r = c.post("/reviews", data={"consent": "1", "name": "", "rating": "5", "text": ""})
        assert r.status_code == 200
        assert "Заполните имя" in r.text


def test_rate_limit_on_review_submission():
    with TestClient(app) as c:
        codes = []
        for i in range(7):
            r = c.post("/reviews", data={"consent": "1", 
                "name": f"У{i}", "rating": "4", "text": "текст отзыва"},
                follow_redirects=False)
            codes.append(r.status_code)
        # часть запросов должна получить страницу с ошибкой лимита (200, не редирект)
        assert 303 in codes and 200 in codes


def test_moderation_requires_platform_auth():
    with TestClient(app) as c:
        r = c.get("/admin/platform/reviews", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/admin/platform/login"


def test_approve_makes_review_public_then_delete_hides_it():
    with TestClient(app) as c:
        c.post("/reviews", data={"consent": "1", 
            "name": "Марина", "club_name": "Клуб Заря", "rating": "5",
            "text": "Пользуемся два месяца, участники довольны."})

        _login(c)
        mod = c.get("/admin/platform/reviews")
        assert "Марина" in mod.text
        # в общей тестовой БД к этому моменту могут быть и другие отзывы на
        # модерации (от предыдущих тестов файла) — ищем кнопку одобрения
        # именно в строке таблицы с «Мариной», а не первую попавшуюся
        row = re.search(r'<tr>(?:(?!</tr>).)*Марина(?:(?!</tr>).)*</tr>',
                        mod.text, re.S)
        assert row, "не нашли строку с отзывом Марины"
        m = re.search(r'action="(/admin/platform/reviews/\d+/approve)"',
                     row.group(0))
        assert m, "не нашли форму одобрения в строке Марины"
        approve_url = m.group(1)
        csrf = _csrf(mod.text)

        r = c.post(approve_url, data={"csrf": csrf}, follow_redirects=False)
        assert r.status_code == 303

        public = c.get("/reviews")
        assert "Марина" in public.text
        assert "Пользуемся два месяца" in public.text

        review_id = approve_url.split("/")[-2]
        delete_url = f"/admin/platform/reviews/{review_id}/delete"
        c.post(delete_url, data={"csrf": csrf})

        public2 = c.get("/reviews")
        assert "Марина" not in public2.text


def test_approve_without_csrf_rejected():
    with TestClient(app) as c:
        c.post("/reviews", data={"consent": "1", 
            "name": "Без CSRF", "rating": "3", "text": "текст"})
        _login(c)
        mod = c.get("/admin/platform/reviews")
        m = re.search(r'action="(/admin/platform/reviews/\d+/approve)"', mod.text)
        r = c.post(m.group(1), data={}, follow_redirects=False)
        assert r.status_code == 403
