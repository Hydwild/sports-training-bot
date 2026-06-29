"""
Точка входа FastAPI.
- Создаёт таблицы при старте (для SQLite/dev; в проде используйте Alembic).
- Запускает фоновые задачи (доставка outbox, планировщик).
- Поднимает REST API и webhook-эндпойнты Telegram и VK.
- При TG_MODE=polling дополнительно запускает Telegram-поллинг как фоновую задачу.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response

from app.api.routes import router as api_router
from app.core.config import settings
from app.core.logging_setup import setup_logging
from app.db.engine import engine
from app.models.entities import Base
from app.services import tasks

setup_logging()
logger = logging.getLogger("app")

_background: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    # таблицы (dev). В проде — alembic upgrade head.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Таблицы готовы. БД: %s",
                "SQLite" if settings.is_sqlite else "PostgreSQL")

    # подключаем ботов (регистрируют senders и, при polling, поллинг)
    from app.bots import telegram as tg
    from app.bots import vk as vk

    await tg.setup()
    await vk.setup()

    _background.append(asyncio.create_task(tasks.deliver_outbox_loop()))
    _background.append(asyncio.create_task(tasks.scheduler_loop()))
    if settings.tg_mode == "polling" and settings.tg_token:
        _background.append(asyncio.create_task(tg.run_polling()))
    if settings.run_vk_polling and settings.vk_token:
        _background.append(asyncio.create_task(vk.run_polling()))

    logger.info("Старт завершён. Фоновых задач: %d", len(_background))
    try:
        yield
    finally:
        for t in _background:
            t.cancel()
        await engine.dispose()
        logger.info("Остановка.")


app = FastAPI(title="Badminton Platform", version="2.0", lifespan=lifespan)
app.include_router(api_router)

# Pro: HTML-админка и платёжные вебхуки. В Lite не подключаются.
if settings.is_pro:
    from app.admin.routes import router as admin_router  # noqa: E402
    app.include_router(admin_router)

    @app.post("/webhook/payment/{provider}")
    async def payment_webhook(provider: str, request: Request):
        """Вебхук платёжного провайдера (yookassa|stripe). Идемпотентно зачисляет оплату."""
        from app.db.engine import SessionLocal
        from app.services import payment_service
        body = await request.body()
        payload = await request.json()
        remote_ip = request.client.host if request.client else None
        async with SessionLocal() as session:
            ok = await payment_service.handle_webhook(
                session, provider, body=body, headers=dict(request.headers),
                remote_ip=remote_ip, payload=payload)
        return {"ok": ok}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "edition": settings.edition,
            "db": "sqlite" if settings.is_sqlite else "postgres"}


# ---------- Telegram webhook ----------

@app.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> Response:
    if settings.tg_webhook_secret and \
            x_telegram_bot_api_secret_token != settings.tg_webhook_secret:
        raise HTTPException(status_code=403, detail="bad secret")
    from app.bots import telegram as tg
    update = await request.json()
    await tg.feed_webhook_update(update)
    return Response(status_code=200)


# ---------- VK Callback API ----------

@app.post("/webhook/vk")
async def vk_webhook(request: Request) -> Response:
    body = await request.json()
    # VK требует ответить строкой подтверждения на событие confirmation
    if body.get("type") == "confirmation":
        return Response(content=settings.vk_confirmation, media_type="text/plain")
    if settings.vk_secret and body.get("secret") != settings.vk_secret:
        raise HTTPException(status_code=403, detail="bad secret")
    from app.bots import vk as vk
    await vk.feed_callback_event(body)
    return Response(content="ok", media_type="text/plain")
