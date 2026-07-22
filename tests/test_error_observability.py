"""
Видно, КАКОЙ эндпойнт отдаёт 4xx/5xx.

График Requests в Railway показывает только доли по кодам, а не маршруты,
и разобраться в источнике ошибок по нему невозможно. Счётчики закрывают
этот пробел — но обязаны считать по ШАБЛОНУ маршрута: в пути живёт
одноразовый токен управления (`/club/{tenant_id}/m/{token}`), и сырые пути
в логах/счётчиках означали бы утечку секрета.
"""
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app

H = {"x-admin-token": "tok"}


@pytest.fixture(autouse=True)
def _clean_counters():
    main_module._ERROR_COUNTS.clear()
    yield
    main_module._ERROR_COUNTS.clear()


def test_client_error_is_counted_by_route_template():
    with TestClient(app) as c:
        c.get("/club/999999")                    # несуществующий клуб -> 404
    keys = list(main_module.error_counters())
    assert any("/club/{tenant_id}" in k and "404" in k for k in keys), keys


def test_unmatched_path_does_not_explode_cardinality():
    """Сканеры дёргают случайные URL — они не должны раздувать словарь."""
    with TestClient(app) as c:
        for i in range(50):
            c.get(f"/nonexistent-{i}")
    counts = main_module.error_counters()
    assert len(counts) == 1, counts
    key, hits = next(iter(counts.items()))
    assert "<нет маршрута>" in key and hits == 50


def test_manage_token_never_reaches_counters():
    """Главное требование: секрет из пути не попадает ни в счётчики, ни в лог."""
    token = "СЕКРЕТНЫЙ-ТОКЕН-УПРАВЛЕНИЯ-12345"
    with TestClient(app) as c:
        r = c.get(f"/club/1/m/{token}")
        assert r.status_code >= 400          # токен недействителен
    dumped = str(main_module.error_counters())
    assert token not in dumped
    # но сам маршрут посчитан — иначе счётчики бесполезны
    assert any("/m/{token}" in k for k in main_module.error_counters())


def test_counters_cap_cardinality(monkeypatch):
    monkeypatch.setattr(main_module, "_ERROR_COUNTS_MAX", 2)

    class _Req:
        method = "GET"
        scope: dict = {}

    for i in range(10):
        req = _Req()
        req.scope = {"route": type("R", (), {"path": f"/route-{i}"})()}
        main_module._record_error(req, 500)

    counts = main_module.error_counters()
    assert len(counts) == 3          # 2 обычных + агрегат "(прочее)"
    assert any(k.startswith("(прочее)") for k in counts)
    assert sum(counts.values()) == 10, "потеряли события при схлопывании"


def test_successful_requests_are_not_counted():
    with TestClient(app) as c:
        assert c.get("/health").status_code in (200, 503)
    counts = main_module.error_counters()
    assert not any("-> 200" in k for k in counts)


def test_counters_visible_to_platform_operator_only():
    with TestClient(app) as c:
        c.get("/club/999999")                    # сгенерировали 404
        c.cookies.clear()
        assert c.get("/admin/platform/diag",
                     follow_redirects=False).status_code in (302, 303, 401, 403)
        c.post("/admin/platform/login", data={"token": "tok"})
        body = c.get("/admin/platform/diag").json()
    assert "error_counts" in body
    assert all(isinstance(v, int) for v in body["error_counts"].values())
