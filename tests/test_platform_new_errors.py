"""
Создание клуба не должно заканчиваться голым «Internal Server Error».

Боевой случай: оператор заводит демо-клуб с токеном бота, а BOT_TOKEN_ENC_KEY
не задан — токен физически нечем зашифровать. Раньше это давало 500 без
единого слова объяснения. Хуже того, клуб к этому моменту УЖЕ создан:
оператор, ничего не поняв, повторяет попытку и плодит дубли.
"""
import re

import pytest
from fastapi.testclient import TestClient

import app.admin.routes as admin_routes
from app.core.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def _http_cookies(monkeypatch):
    # TestClient ходит по http; без этого cookie с флагом Secure не уедет
    monkeypatch.setattr(admin_routes, "_cookie_secure", lambda: False)
    # вход в панель ограничен 5 попытками за 5 минут, а счётчик общий на весь
    # прогон: без сброса эти тесты падают только в компании других
    from app.api import rate_limit
    rate_limit._memory.clear()
    yield
    rate_limit._memory.clear()


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/admin/platform/login", data={"token": "tok"})
        yield c


def _csrf(c) -> str:
    page = c.get("/admin/platform/new")
    m = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert m, "форма создания недоступна"
    return m.group(1)


def _plain(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _create(c, **extra):
    data = {"csrf": _csrf(c), "club_name": "Демо-салон",
            "timezone": "Europe/Moscow", "vertical": "beauty", "is_demo": "1"}
    data.update(extra)
    return c.post("/admin/platform/new", data=data, follow_redirects=False)


def test_missing_token_key_explains_itself(client, monkeypatch):
    """Вместо «Internal Server Error» — причина и что делать."""
    monkeypatch.setattr(settings, "bot_token_enc_key", "")
    monkeypatch.setattr(settings, "bot_token_keys", "")
    monkeypatch.setattr(settings, "bot_token_keyring", "")

    r = _create(client, tg_token="111:AAA", tg_delivery_mode="polling")
    text = _plain(r.text)
    assert "BOT_TOKEN_ENC_KEY" in text
    assert "Internal Server Error" not in text


def test_operator_is_warned_that_club_already_exists(client, monkeypatch):
    """Ключевое: клуб создан. Без этой строки оператор повторяет попытку и
    заводит дубль."""
    monkeypatch.setattr(settings, "bot_token_enc_key", "")
    monkeypatch.setattr(settings, "bot_token_keys", "")
    monkeypatch.setattr(settings, "bot_token_keyring", "")

    text = _plain(_create(client, tg_token="111:AAA",
                          tg_delivery_mode="polling").text)
    assert "создан" in text
    assert "Повторно создавать клуб НЕ нужно" in text
    assert re.search(r"id=\d+", text), "не назван id созданного клуба"


def test_token_value_never_echoed_back(client, monkeypatch):
    """Сообщение об ошибке не должно возвращать сам токен на страницу."""
    monkeypatch.setattr(settings, "bot_token_enc_key", "")
    monkeypatch.setattr(settings, "bot_token_keys", "")
    monkeypatch.setattr(settings, "bot_token_keyring", "")

    token = "999888:СЕКРЕТНЫЙ-ТОКЕН"
    r = _create(client, tg_token=token, tg_delivery_mode="polling")
    assert token not in r.text


def test_creation_without_token_still_works(client):
    """Обычный путь не сломан: без токена клуб заводится штатно."""
    assert _create(client).status_code == 200


def test_creation_with_valid_key_works(client, monkeypatch):
    monkeypatch.setattr(settings, "bot_token_enc_key", "ключ-шифрования-токенов")
    r = _create(client, tg_token="111:AAA", tg_delivery_mode="polling")
    assert r.status_code == 200
    assert "Internal Server Error" not in r.text


# ---------- занятый короткий адрес ----------

def _save_slug(c, tenant_id: int, slug: str, name: str = "Клуб"):
    page = c.get(f"/admin/platform/{tenant_id}/edit")
    m = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert m, "форма клуба недоступна"
    return c.post(f"/admin/platform/{tenant_id}/edit", data={
        "csrf": m.group(1), "club_name": name, "timezone": "Europe/Moscow",
        "vertical": "sport", "slug": slug})


def _new_club(c, name: str) -> int:
    return c.post("/api/tenants", json={"name": name},
                  headers={"x-admin-token": "tok"}).json()["id"]


def test_duplicate_slug_explains_itself(client):
    """Боевой случай: короткий адрес уже занят. Уникальность стоит на
    уровне БД, и раньше конфликт прилетал оператору голым 500."""
    a = _new_club(client, "Клуб Первый")
    b = _new_club(client, "Клуб Второй")

    assert _save_slug(client, a, "zanyatyj-adres").status_code == 200
    r = _save_slug(client, b, "zanyatyj-adres")

    assert r.status_code == 400, "конфликт ввода — не сбой сервера"
    text = _plain(r.text)
    assert "занят" in text
    assert "Internal Server Error" not in text


def test_form_still_works_after_a_conflict(client):
    """После неудачного commit сессия непригодна: без rollback падал бы уже
    следующий запрос к ней."""
    a = _new_club(client, "Клуб Третий")
    b = _new_club(client, "Клуб Четвёртый")
    _save_slug(client, a, "adres-treti")
    _save_slug(client, b, "adres-treti")            # конфликт

    # форма открывается и принимает другой адрес
    assert client.get(f"/admin/platform/{b}/edit").status_code == 200
    assert _save_slug(client, b, "adres-chetvyorty").status_code == 200


def test_conflict_does_not_change_the_club(client):
    """Клуб должен остаться прежним, а не сохраниться наполовину."""
    import asyncio

    a = _new_club(client, "Клуб Пятый")
    b = _new_club(client, "Клуб Шестой")
    _save_slug(client, a, "adres-pyaty")
    _save_slug(client, b, "adres-pyaty", name="Переименованный")

    async def name_of() -> str:
        from app.db.engine import SessionLocal, engine
        from app.models.entities import Tenant
        await engine.dispose()
        async with SessionLocal() as s:
            return (await s.get(Tenant, b)).name

    assert asyncio.run(name_of()) == "Клуб Шестой"


def test_freeing_a_slug_lets_another_club_take_it(client):
    a = _new_club(client, "Клуб Седьмой")
    b = _new_club(client, "Клуб Восьмой")
    _save_slug(client, a, "obshiy-adres")
    assert _save_slug(client, b, "obshiy-adres").status_code == 400

    _save_slug(client, a, "")                        # освободили
    assert _save_slug(client, b, "obshiy-adres").status_code == 200
