"""
Валидация адресов внешних изображений (фото мастеров, обложка клуба).

Адрес попадает в <img src> публичной страницы: браузер посетителя пойдёт к
нему сам. Отсюда запреты — только https, никаких локальных и внутренних
адресов (защита от XSS, смешанного контента и запросов во внутреннюю сеть).
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core.image_url import validate_image_url
from app.main import app

H = {"x-admin-token": "tok"}


@pytest.fixture(autouse=True)
def _clean():
    from app.api import rate_limit
    rate_limit._memory.clear()
    yield
    rate_limit._memory.clear()


# ---------- сам валидатор ----------

def test_https_public_url_accepted():
    assert validate_image_url("https://cdn.example.com/a.jpg") \
        == "https://cdn.example.com/a.jpg"
    assert validate_image_url("") is None
    assert validate_image_url(None) is None


@pytest.mark.parametrize("url", [
    "http://example.com/a.jpg",          # plain http
    "javascript:alert(1)",               # XSS-схема
    "data:image/png;base64,iVBOR",       # data:
    "file:///etc/passwd",                # локальный файл
    "ftp://example.com/a.jpg",           # не http-схема
])
def test_dangerous_schemes_rejected(url):
    with pytest.raises(ValueError):
        validate_image_url(url)


@pytest.mark.parametrize("host", [
    "localhost",
    "127.0.0.1",
    "10.0.0.5",                          # private
    "192.168.1.10",                      # private
    "172.16.0.1",                        # private
    "169.254.169.254",                   # облачный метадата-эндпоинт
    "[::1]",                             # IPv6 loopback
    "[fc00::1]",                         # IPv6 unique-local
    "0.0.0.0",
])
def test_internal_hosts_rejected(host):
    with pytest.raises(ValueError):
        validate_image_url(f"https://{host}/a.jpg")


def test_too_long_url_rejected():
    with pytest.raises(ValueError):
        validate_image_url("https://x.com/" + "a" * 600)


# ---------- через формы ----------

def test_master_photo_url_rejected_on_bad_host():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб Фото"},
                     headers=H).json()["id"]
        r = c.post(f"/api/tenants/{tid}/masters", headers=H, json={
            "name": "Мастер", "photo_url": "http://127.0.0.1/x.jpg"})
        assert r.status_code == 422       # схема отвергает http и loopback

        ok = c.post(f"/api/tenants/{tid}/masters", headers=H, json={
            "name": "Мастер2", "photo_url": "https://cdn.example.com/p.jpg"})
        assert ok.status_code == 200


def test_external_images_carry_no_referrer_and_lazy():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Клуб Витрины", "vertical": "beauty"},
            headers=H).json()["id"]
        c.post(f"/api/tenants/{tid}/masters", headers=H, json={
            "name": "Наталья", "photo_url": "https://cdn.example.com/n.jpg"})
        import datetime as dt
        start = (dt.datetime.now(dt.timezone.utc)
                 + dt.timedelta(days=2)).isoformat()
        mid = c.get(f"/api/tenants/{tid}/masters", headers=H).json()[0]["id"]
        c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
            "title": "Стрижка", "start_at": start, "max_participants": 1,
            "master_id": mid})

        page = c.get(f"/club/{tid}").text
        imgs = re.findall(r'<img[^>]*cdn\.example\.com[^>]*>', page)
        assert imgs, "фото мастера не отрендерилось"
        for tag in imgs:
            assert 'referrerpolicy="no-referrer"' in tag
            assert 'loading="lazy"' in tag
