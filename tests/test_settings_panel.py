"""Страница настроек админки и проверка прав (один TestClient на сессию)."""

import re

from fastapi.testclient import TestClient
from app.main import app

H = {"X-Admin-Token": "tok"}


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def test_settings_panel_flow():
    with TestClient(app) as c:
        # клуб + coach + assistant
        tid = c.post("/api/tenants", json={"name": "К"}, headers=H).json()["id"]
        c.post(f"/api/tenants/{tid}/members",
               json={"tg_user_id": 777, "role": "coach"}, headers=H)
        c.post(f"/api/tenants/{tid}/members",
               json={"tg_user_id": 888, "role": "assistant"}, headers=H)

        # coach открывает и сохраняет настройки
        login = c.post("/admin/auth/dev", data={"tg_user_id": 777},
                       follow_redirects=False)
        c.cookies.set("access_token", login.cookies["access_token"])
        page = c.get("/admin/settings")
        assert page.status_code == 200
        r = c.post("/admin/settings", data={
            "csrf": _csrf(page.text),
            "brand_name": "Брендовый Клуб", "brand_color": "#112233",
            "reminder_enabled": "on", "reminder_minutes": "90",
            "guest_reminder_minutes": "180",
            "guest_expire_enabled": "on", "guest_expire_minutes": "45",
            "publish_notify_enabled": "on", "cancel_lock_minutes": "120",
        })
        assert r.status_code == 200 and "Сохранено" in r.text
        # без csrf — отказ
        assert c.post("/admin/settings", data={"brand_name": "X"}).status_code == 403
        r = c.get("/admin/settings")
        assert "90" in r.text and "120" in r.text and "Брендовый Клуб" in r.text

        # assistant не имеет доступа к настройкам
        login2 = c.post("/admin/auth/dev", data={"tg_user_id": 888},
                        follow_redirects=False)
        c.cookies.set("access_token", login2.cookies["access_token"])
        assert c.get("/admin/settings").status_code == 403
