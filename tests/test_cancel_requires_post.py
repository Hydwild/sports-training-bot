"""
Отмена записи не должна происходить от простого перехода по ссылке.

Ссылку отмены видит не только человек: Telegram и ВК открывают её ради
превью, браузеры делают предзагрузку, антивирусы и корпоративные фильтры
проверяют содержимое. Раньше любое такое обращение отменяло запись.
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


def _booked(c):
    tid = c.post("/api/tenants", json={"name": "Клуб Отмены"},
                 headers=H).json()["id"]
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    tr = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": "Игра", "start_at": start, "max_participants": 5,
    }).json()["id"]
    r = c.post(f"/club/{tid}/signup", data={
        "consent": "1", "training_id": tr, "name": "Игорь",
        "phone": "79120001133"})
    link = re.search(r'href="(/club/\d+/cancel\?[^"]+)"', r.text).group(1)
    return tid, tr, link.replace("&amp;", "&")


def _still_booked(c, tid, tr) -> bool:
    signups = c.get(f"/api/tenants/{tid}/trainings/{tr}/signups",
                    headers=H).json()
    return any(s["name"] == "Игорь" and s["status"] == "active"
               for s in signups)


def test_get_only_asks_and_keeps_booking():
    with TestClient(app) as c:
        tid, tr, link = _booked(c)

        page = c.get(link)
        assert page.status_code == 200
        assert "Отменить запись?" in page.text
        assert "Игра" in page.text            # что именно отменяем
        assert _still_booked(c, tid, tr), "переход по ссылке отменил запись"

        # даже несколько «превью-заходов» подряд ничего не меняют
        for _ in range(3):
            c.get(link)
        assert _still_booked(c, tid, tr)


def test_post_cancels_and_bad_token_rejected():
    with TestClient(app) as c:
        tid, tr, link = _booked(c)
        fields = dict(re.findall(r'name="(\w+)" value="([^"]+)"',
                                 c.get(link).text))
        url = link.split("?")[0]

        assert c.post(url, data={**fields, "s": "0" * 32}).status_code == 403
        assert _still_booked(c, tid, tr)

        done = c.post(url, data=fields)
        assert done.status_code == 200
        assert "отменена" in done.text
        assert not _still_booked(c, tid, tr)
