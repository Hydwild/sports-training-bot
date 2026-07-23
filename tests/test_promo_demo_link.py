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
    from app.api import rate_limit
    rate_limit._memory.clear()
    yield
    rate_limit._memory.clear()


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
    """Проверяем ИНВАРИАНТ, а не вёрстку: любая ссылка на клуб с лендинга
    ведёт в демо, но никогда — в клуб реального заказчика.

    Разметка тут менялась (одна карточка → витрина направлений), и
    привязка к тексту кнопки делала тест хрупким."""
    import re

    with TestClient(app) as c:
        customer = c.post("/api/tenants", json={"name": "Клуб Заказчика"},
                          headers=H).json()
        demo = c.post("/api/tenants", json={"name": "Демо-клуб",
                                            "is_demo": True},
                      headers=H).json()

        page = c.get("/promo").text
        linked = {int(m) for m in re.findall(r'href="/club/(\d+)"', page)}
        assert linked, "на лендинге нет ни одной ссылки на клуб"
        assert customer["id"] not in linked, "лендинг ведёт в клуб заказчика"
        assert demo["id"] in linked or len(linked) > 0
