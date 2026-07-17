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


async def _ensure_columns(conn) -> None:
    """
    Добавляет недостающие колонки в существующие таблицы (лёгкая авто-миграция).
    Нужна, потому что create_all не изменяет уже созданные таблицы.
    Список пополняется по мере добавления полей в модели.
    """
    from sqlalchemy import text
    # (таблица, колонка, SQL-тип)
    wanted = [
        ("subscribers", "alias", "VARCHAR(200)"),
        ("tenants", "welcome_text", "VARCHAR(1000)"),
        ("tenants", "signup_close_minutes", "INTEGER DEFAULT 0"),
        ("tenants", "paid_until", "VARCHAR(10) DEFAULT \'\'"),
        ("tenants", "tg_token", "VARCHAR(200)"),
        ("tenants", "vk_token", "VARCHAR(200)"),
        ("trainings", "group_message_id", "BIGINT"),
        ("outbox", "attempts", "INTEGER DEFAULT 0"),
    ]
    for table, column, coltype in wanted:
        try:
            # ADD COLUMN IF NOT EXISTS поддерживается Postgres и новыми SQLite;
            # на старых SQLite ловим ошибку и проверяем вручную.
            await conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}"))
        except Exception:
            # запасной путь: пробуем без IF NOT EXISTS, ошибку «уже есть» глушим
            try:
                await conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
            except Exception:
                pass  # колонка уже существует


async def _scrub_legacy_photo_urls(conn) -> None:
    """
    Одноразовая очистка: раньше аватар Telegram сохранялся как URL с токеном
    бота внутри (https://api.telegram.org/file/bot<TOKEN>/...). Теперь токен
    не хранится (см. user_info.fetch_tg_photo_ref) — вычищаем legacy-значения,
    чтобы секрет не оставался в БД и бэкапах. Идемпотентно.
    """
    from sqlalchemy import text
    prefix = "https://api.telegram.org/file/bot%"
    for table in ("subscribers", "signups"):
        try:
            await conn.execute(
                text(f"UPDATE {table} SET photo_url = NULL "
                     "WHERE photo_url LIKE :p"), {"p": prefix})
        except Exception:
            pass  # колонки/таблицы может не быть в этой редакции


@asynccontextmanager
async def lifespan(app: FastAPI):
    # не даём стартовать с небезопасными дефолтами (JWT/webhook-секреты)
    settings.assert_production_secrets()
    # таблицы (dev). В проде — alembic upgrade head.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # безопасно добавляем новые колонки, если их ещё нет в существующей БД
        await _ensure_columns(conn)
        # вычищаем legacy-аватары с токеном бота внутри URL
        await _scrub_legacy_photo_urls(conn)
    logger.info("Таблицы готовы. БД: %s",
                "SQLite" if settings.is_sqlite else "PostgreSQL")

    # подключаем ботов (регистрируют senders и, при polling, поллинг)
    from app.bots import telegram as tg
    from app.bots import vk as vk

    await tg.setup()
    await vk.setup()

    import os as _os
    if _os.getenv("DISABLE_BACKGROUND") != "1":
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


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    # HSTS — только для https (за TLS-прокси проверяем X-Forwarded-Proto)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if proto == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


app.include_router(api_router)
from app.api.routes import public_router  # noqa: E402
app.include_router(public_router)

# Pro: HTML-админка и платёжные вебхуки. В Lite не подключаются.
if settings.is_pro:
    from app.admin.routes import router as admin_router  # noqa: E402
    app.include_router(admin_router)
    from app.admin.platform import router as platform_router  # noqa: E402
    app.include_router(platform_router)

    @app.post("/webhook/payment/{provider}")
    async def payment_webhook(provider: str, request: Request):
        """Вебхук платёжного провайдера (yookassa|stripe). Идемпотентно зачисляет оплату."""
        from app.db.engine import SessionLocal
        from app.services import payment_service
        body = await request.body()
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="bad payload")
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
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad payload")
    # VK требует ответить строкой подтверждения на событие confirmation
    if body.get("type") == "confirmation":
        return Response(content=settings.vk_confirmation, media_type="text/plain")
    import hmac as _hmac
    # Секрет обязателен: без него любой мог бы подделать событие VK Callback API.
    if not settings.vk_secret:
        logger.error("VK webhook отклонён: VK_SECRET не задан. "
                     "Укажите секрет в настройках Callback API и в VK_SECRET.")
        raise HTTPException(status_code=403, detail="webhook secret not configured")
    if not _hmac.compare_digest(str(body.get("secret") or ""), settings.vk_secret):
        raise HTTPException(status_code=403, detail="bad secret")
    from app.bots import vk as vk
    await vk.feed_callback_event(body)
    return Response(content="ok", media_type="text/plain")
