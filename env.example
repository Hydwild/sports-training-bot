"""Интеграция через TestClient: роли, admin-доступ, white-label, dev-логин."""

import pytest
from fastapi.testclient import TestClient
from app.main import app

H = {"X-Admin-Token": "tok"}


def test_full_admin_flow():
    with TestClient(app) as c:
        # создаём клуб
        r = c.post("/api/tenants", json={"name": "Клуб А"}, headers=H)
        assert r.status_code == 200
        tid = r.json()["id"]

        # назначаем owner
        r = c.post(f"/api/tenants/{tid}/members",
                   json={"tg_user_id": 555, "role": "owner", "name": "Босс"}, headers=H)
        assert r.status_code == 200 and r.json()["role"] == "owner"

        # white-label
        r = c.patch(f"/api/tenants/{tid}/brand",
                    json={"brand_name": "Мой Клуб", "brand_color": "#ff0066"}, headers=H)
        assert r.status_code == 200

        # тренировка с ценой
        r = c.post(f"/api/tenants/{tid}/trainings", json={
            "title": "T", "start_at": "2026-12-01T19:00:00+00:00",
            "max_participants": 8, "price_minor": 50000}, headers=H)
        assert r.status_code == 200

        # dev-логин владельца -> редирект на /admin, ставится cookie
        r = c.post("/admin/auth/dev", data={"tg_user_id": 555},
                   follow_redirects=False)
        assert r.status_code == 302
        assert "access_token" in r.cookies

        # дашборд доступен с этой сессией, брендинг применён
        c.cookies.set("access_token", r.cookies["access_token"])
        r = c.get("/admin")
        assert r.status_code == 200
        assert "Мой Клуб" in r.text

        # неизвестный пользователь не входит
        r = c.post("/admin/auth/dev", data={"tg_user_id": 999},
                   follow_redirects=False)
        assert r.status_code == 403

