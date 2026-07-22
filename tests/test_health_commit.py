"""
/health отдаёт SHA развёрнутого коммита — чтобы проверять «задеплоилось или
нет», а не угадывать.
"""
from fastapi.testclient import TestClient

from app.main import app


def test_health_reports_commit():
    with TestClient(app) as c:
        data = c.get("/health").json()
        assert "commit" in data
        assert data["commit"]                    # не пусто
        assert data["status"] == "ok"


def test_commit_comes_from_env(monkeypatch):
    from app.core import version
    version.commit_sha.cache_clear()
    monkeypatch.setenv("GIT_SHA", "deadbeefcafe0000")
    assert version.commit_sha() == "deadbeefcafe"   # обрезано до 12
    version.commit_sha.cache_clear()


def test_railway_var_is_honored(monkeypatch):
    from app.core import version
    version.commit_sha.cache_clear()
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123def456789")
    assert version.commit_sha() == "abc123def456"
    version.commit_sha.cache_clear()
