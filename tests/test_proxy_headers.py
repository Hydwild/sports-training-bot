"""
Интеграция с обратным прокси (Railway/Render).

Доверие к X-Forwarded-For — вопрос развёртывания, а не обработчика: в
проде uvicorn запускается с --proxy-headers --forwarded-allow-ips (см.
start.sh, переменная TRUSTED_PROXIES). Здесь проверяем связку целиком:
за ДОВЕРЕННЫМ прокси реальный адрес клиента подставляется в
request.client.host (и попадает в лимит), а от НЕдоверенного источника
заголовок игнорируется — подделать свой адрес нельзя.
"""
import httpx
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.routes import client_ip
from fastapi import FastAPI, Request


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request):
        # ровно та функция, что кормит rate limit
        return {"ip": client_ip(request)}

    return app


async def _get_ip(app, xff: str | None) -> str:
    headers = {"x-forwarded-for": xff} if xff else {}
    transport = httpx.ASGITransport(app=app, client=("10.9.8.7", 12345))
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as c:
        r = await c.get("/whoami", headers=headers)
    return r.json()["ip"]


async def test_trusted_proxy_forwards_real_client_ip():
    """С --forwarded-allow-ips='*' (доверяем прокси) middleware подставляет
    первый адрес из X-Forwarded-For в client.host."""
    app = ProxyHeadersMiddleware(_probe_app(), trusted_hosts="*")
    ip = await _get_ip(app, "203.0.113.50, 10.0.0.1")
    assert ip == "203.0.113.50"


async def test_untrusted_source_cannot_spoof_ip():
    """Без доверия к источнику заголовок игнорируется — остаётся адрес
    самого соединения, подделать нельзя."""
    # доверяем только конкретному прокси, а «клиент» приходит с другого адреса
    app = ProxyHeadersMiddleware(_probe_app(), trusted_hosts="192.0.2.1")
    ip = await _get_ip(app, "203.0.113.50")
    assert ip == "10.9.8.7"      # реальный peer, а не подделанный заголовок


async def test_no_middleware_ignores_header():
    """Голое приложение (как в тестах) вообще не смотрит на заголовок."""
    app = _probe_app()
    ip = await _get_ip(app, "203.0.113.99")
    assert ip == "10.9.8.7"


def test_start_sh_passes_proxy_flags():
    """Связка настроена в start.sh, а не в коде обработчика."""
    import pathlib
    sh = pathlib.Path(__file__).resolve().parent.parent / "start.sh"
    text = sh.read_text(encoding="utf-8")
    assert "--proxy-headers" in text
    assert "--forwarded-allow-ips" in text
    assert "TRUSTED_PROXIES" in text
