"""
Ссылка «Страница записи» на лендинге.

Регресс: адрес был захардкожен как /club/1 — страница РЕАЛЬНОГО заказчика.
Посторонние попадали на чужой клуб, а выглядел он как заброшенное демо.
"""
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


def test_promo_never_links_to_hardcoded_club():
    from app.api.promo_page import PROMO_HTML
    assert 'href="/club/1"' not in PROMO_HTML


def test_demo_card_hidden_without_demo_club(monkeypatch):
    from app.api.promo_page import render_promo_page
    html = render_promo_page(None)
    assert "/club/" not in html
    assert "Открыть страницу" not in html        # кнопка демо-карточки
    assert "<!--DEMO_CLUB_CARD-->" not in html    # заглушка не протекла


def test_demo_card_points_to_demo_tenant():
    import re

    with TestClient(app) as c:
        customer = c.post("/api/tenants", json={"name": "Клуб Заказчика"},
                          headers=H).json()
        c.post("/api/tenants", json={"name": "Демо-клуб", "is_demo": True},
               headers=H)

        page = c.get("/promo").text
        assert "Открыть страницу" in page
        linked = re.search(r'href="/club/(\d+)">Открыть страницу', page)
        assert linked, "нет ссылки на демо-клуб"
        # ссылаемся на демо-клуб, а не на клуб заказчика
        assert int(linked.group(1)) != customer["id"]
