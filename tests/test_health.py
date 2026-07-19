"""/health: реальная проверка БД (SELECT 1), а не статичный ответ."""
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


def test_health_ok_when_db_reachable():
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "edition" in body and "db" in body


def test_health_returns_503_when_db_unreachable(monkeypatch):
    class _BrokenConn:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *a):
            return False

    class _BrokenEngine:
        def connect(self):
            return _BrokenConn()

        async def dispose(self):
            pass

    with TestClient(app) as c:
        monkeypatch.setattr(main_module, "engine", _BrokenEngine())
        r = c.get("/health")
        assert r.status_code == 503
        assert r.json()["status"] == "error"
