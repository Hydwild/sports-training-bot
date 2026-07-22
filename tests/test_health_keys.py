"""
/health отражает читаемость ключей телефонов ПО РЕАЛЬНЫМ СТРОКАМ БД.

Подменённый секрет под формально существующей версией конфигурацию проходит
(версия jwt всегда выводится из текущего JWT_SECRET) — поймать его можно
только расшифровав реальную строку. Такой случай обязан давать keys_ok=false
и 503, а не бодрое "ok". Ошибка startup-проверки при этом не проглатывается:
она попадает и в лог, и в /health.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.core import phones
from app.main import app

H = {"x-admin-token": "tok"}


@pytest.fixture(autouse=True)
def _clean():
    from app.api import rate_limit
    rate_limit._memory.clear()
    yield
    rate_limit._memory.clear()


def _seed_web_customer(c, phone: str) -> int:
    """Настоящая веб-запись: в web_customers появляется строка, зашифрованная
    текущим ключом версии jwt."""
    tid = c.post("/api/tenants", json={"name": "Клуб Здоровья"},
                 headers=H).json()["id"]
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    tr = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": "Занятие", "start_at": start, "max_participants": 5,
    }).json()["id"]
    r = c.post(f"/club/{tid}/signup", data={
        "consent": "1", "training_id": tr, "name": "Клиент", "phone": phone})
    assert r.status_code == 200
    return tid


def test_health_ok_when_keys_readable():
    with TestClient(app) as c:
        _seed_web_customer(c, "79230001111")
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["keys_ok"] is True
        assert r.json()["status"] == "ok"


def test_health_503_when_db_row_key_is_wrong(monkeypatch):
    """JWT_SECRET сменили, старый ключ под меткой jwt не сохранили.
    Конфигурация формально валидна — ловит только сверка по строкам БД."""
    with TestClient(app) as c:
        _seed_web_customer(c, "79230002222")
        assert c.get("/health").json()["keys_ok"] is True

        monkeypatch.setattr(phones.settings, "jwt_secret",
                            "совсем-другой-секрет-достаточной-длины")
        r = c.get("/health")
        assert r.status_code == 503
        assert r.json()["keys_ok"] is False
        assert r.json()["status"] == "error"


def test_health_recovers_when_old_key_restored(monkeypatch):
    """Вернули старый секрет под меткой jwt — /health снова здоров. Значит
    keys_ok отражает текущее состояние данных, а не разовый вердикт."""
    with TestClient(app) as c:
        _seed_web_customer(c, "79230005555")
        good = phones.settings.jwt_secret
        monkeypatch.setattr(phones.settings, "jwt_secret", "подменённый-секрет-1")
        assert c.get("/health").status_code == 503
        # старое значение возвращают именно через keyring, не откатом JWT
        monkeypatch.setattr(phones.settings, "phone_keys", f"jwt:{good}")
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["keys_ok"] is True


def test_health_503_on_broken_key_config(monkeypatch):
    """Конфликт секретов одной версии — keys_ok=false ещё до обращения к
    данным."""
    with TestClient(app) as c:
        monkeypatch.setattr(phones.settings, "phone_keys", "v2:AAA,v2:BBB")
        r = c.get("/health")
        assert r.status_code == 503
        assert r.json()["keys_ok"] is False


def test_health_reports_no_secrets():
    """Диагностика /health не раскрывает ни секретов, ни адресов прокси."""
    with TestClient(app) as c:
        body = c.get("/health").json()
        assert set(body) == {"status", "edition", "db", "commit",
                             "proxy_headers_configured", "keys_ok", "rss_mb"}
        assert phones.settings.jwt_secret not in str(body)
        # rss_mb — только число (или None вне Linux), не строка с путями
        assert body["rss_mb"] is None or isinstance(body["rss_mb"], (int, float))


def test_startup_key_check_is_not_swallowed(monkeypatch, caplog):
    """Раньше результат startup-проверки только писался в лог и терялся.
    Теперь он и логируется, и виден снаружи как keys_ok=false / 503."""
    with TestClient(app) as c:
        _seed_web_customer(c, "79230003333")

    monkeypatch.setattr(phones.settings, "jwt_secret",
                        "ещё-один-другой-секрет-подлиннее")
    with caplog.at_level("ERROR"):
        with TestClient(app) as c:     # заново проходим lifespan
            r = c.get("/health")
    assert r.status_code == 503
    assert r.json()["keys_ok"] is False
    assert "КЛЮЧИ ТЕЛЕФОНОВ" in caplog.text
    assert "jwt" in caplog.text
