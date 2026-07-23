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
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response

from app.api.routes import router as api_router
from app.core.config import settings
from app.core.security import NotAuthenticated
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

    ТОЛЬКО для Lite (SQLite на диске у клиента): там нет ни alembic-истории,
    ни возможности прогнать миграцию перед стартом. В Pro схему ведёт
    alembic — самодеятельность на старте там скрывала бы незавершённые
    миграции: приложение поднималось бы на схеме, до которой upgrade не
    доехал, и расхождение всплывало бы позже и в неожиданном месте.
    """
    from sqlalchemy import text

    if not settings.is_sqlite:
        return
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
        ("tenants", "last_billing_notice", "VARCHAR(32) DEFAULT \'\'"),
        ("tenants", "is_demo", "BOOLEAN DEFAULT 0"),
        ("tenants", "vertical", "VARCHAR(20) DEFAULT 'sport'"),
        ("trainings", "master_id", "INTEGER"),
        ("tenants", "last_digest_date", "VARCHAR(10) DEFAULT \'\'"),
        ("tenants", "cover_url", "VARCHAR(500)"),
        ("tenants", "about", "VARCHAR(2000)"),
        ("tenants", "address", "VARCHAR(300)"),
        ("tenants", "contact_phone", "VARCHAR(32)"),
        ("masters", "bio", "VARCHAR(500) DEFAULT ''"),
        ("tenants", "tg_delivery_mode", "VARCHAR(16) DEFAULT 'polling'"),
        ("tenants", "vk_delivery_mode", "VARCHAR(16) DEFAULT 'longpoll'"),
        ("tenants", "vk_confirmation_code", "VARCHAR(128) DEFAULT ''"),
        ("tenants", "site_url", "VARCHAR(500)"),
        ("tenants", "slug", "VARCHAR(40)"),
        ("tenants", "bot_username", "VARCHAR(64)"),
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


async def _assert_client_webhook_config() -> None:
    """Fail-fast, если в БД уже есть webhook-клиенты без общих настроек."""
    from sqlalchemy import func, or_, select

    from app.db.engine import SessionLocal
    from app.models.entities import Tenant

    async with SessionLocal() as session:
        count = (await session.execute(
            select(func.count()).select_from(Tenant).where(or_(
                Tenant.tg_delivery_mode == "webhook",
                Tenant.vk_delivery_mode == "callback",
            ))
        )).scalar_one()
    if not count:
        return
    if len(settings.webhook_master_secret or "") < 32:
        raise RuntimeError(
            "В БД есть клиентские webhook, но WEBHOOK_MASTER_SECRET "
            "не задан или короче 32 символов"
        )
    base = (settings.public_base_url or "").strip().lower()
    if not base.startswith("https://"):
        raise RuntimeError(
            "В БД есть клиентские webhook, но PUBLIC_BASE_URL не использует https://"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # не даём стартовать с небезопасными дефолтами (JWT/webhook-секреты)
    settings.assert_production_secrets()
    # прокси-заголовки: за прокси без доверенного списка все посетители
    # делят один адрес и общий лимит — в проде это боевой дефект
    settings.assert_proxy_config()
    # конфигурация ключей: конфликт неизменяемых версий или отсутствующая
    # активная версия — фейл на старте (fail-fast), а не молчаливая потеря
    from app.core import bot_tokens, phones
    phones.assert_config_valid()
    bot_tokens.assert_config_valid()
    # таблицы (dev). В проде — alembic upgrade head.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # безопасно добавляем новые колонки, если их ещё нет в существующей БД
        await _ensure_columns(conn)
        # вычищаем legacy-аватары с токеном бота внутри URL
        await _scrub_legacy_photo_urls(conn)
    logger.info("Таблицы готовы. БД: %s",
                "SQLite" if settings.is_sqlite else "PostgreSQL")
    await _assert_client_webhook_config()

    # readiness ключей телефонов по РЕАЛЬНЫМ данным. Результат НЕ проглатываем:
    # он влияет на /health (keys_ok=false → 503), а не только пишется в лог.
    # Жёсткий отказ на создании клиента остаётся отдельно (fail-closed там, где
    # рождается дубль), чтобы не гасить здоровые клубы целиком. Старт при этом
    # не валим: контейнер поднимется, но /health честно покажет нездоровье.
    bad = await _verify_keys_db()
    if bad:
        logger.error("КЛЮЧИ ТЕЛЕФОНОВ: не читаются версии %s — /health вернёт "
                     "keys_ok=false (503), новые веб-клиенты для затронутых "
                     "клубов создаваться не будут. Проверьте PHONE_KEYS/"
                     "PHONE_KEYRING.", bad)
    elif bad is None:
        logger.error("КЛЮЧИ ТЕЛЕФОНОВ: сверка по данным не выполнена — "
                     "/health вернёт keys_ok=false до успешной сверки.")

    # подключаем ботов (регистрируют senders и, при polling, поллинг)
    from app.bots import telegram as tg
    from app.bots import vk as vk

    await tg.setup()
    await vk.setup()

    import os as _os
    if _os.getenv("DISABLE_BACKGROUND") != "1":
        from app.services import inbound
        # Telegram хранит webhook на своей стороне. Сначала сверяем его
        # с TG_MODE и только потом запускаем polling-координатор. Так
        # смена режима не требует ручного вызова Bot API.
        if not await tg.configure_global_delivery():
            # Telegram сейчас недоступен: старт не валим (иначе чужой сбой
            # решает, поднимется ли сервис), но и немым бот не оставляем —
            # дотягиваем режим доставки в фоне.
            _background.append(asyncio.create_task(
                tg.sync_global_delivery_loop()))
        _background.append(asyncio.create_task(tasks.deliver_outbox_loop()))
        _background.append(asyncio.create_task(tasks.scheduler_loop()))
        _background.append(asyncio.create_task(inbound.worker_loop()))
        _background.append(asyncio.create_task(
            tasks.supervise("Telegram-поллинг", tg.run_polling)))
        if settings.run_vk_polling:
            _background.append(asyncio.create_task(
                tasks.supervise("VK-поллинг", vk.run_polling)))

    logger.info("Старт завершён. Фоновых задач: %d", len(_background))
    try:
        yield
    finally:
        for t in _background:
            t.cancel()
        if _background:
            await asyncio.gather(*_background, return_exceptions=True)
            _background.clear()
        await tg.shutdown()
        await vk.shutdown()
        await engine.dispose()
        logger.info("Остановка.")


app = FastAPI(title="Badminton Platform", version="2.0", lifespan=lifespan)


@app.exception_handler(NotAuthenticated)
async def _not_authenticated_handler(request: Request, exc: NotAuthenticated):
    """Заход на защищённую HTML-страницу (/admin, /admin/platform) без
    активной сессии — вместо голого JSON 401 вежливо перекидываем на
    соответствующую страницу входа. JSON API (/api/*) сюда не попадает —
    там своя авторизация (X-Admin-Token) и обычный HTTPException."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(exc.redirect_to, status_code=302)


# Content-Security-Policy.
#
# Честная оговорка: страницы приложения используют ВСТРОЕННЫЕ <style> и
# небольшие <script> (шаблоны /club, /promo, /faq, админка). Поэтому в
# политике оставлены 'unsafe-inline' для style-src и script-src — без них
# страницы просто не отрисуются, а переход на nonce для каждой инлайновой
# вставки — отдельная крупная работа. Но остальные векторы закрыты:
#   object-src 'none'      — нет Flash/апплетов;
#   base-uri 'self'        — нельзя переписать базовый URL и увести ссылки;
#   frame-ancestors 'none' — страницу нельзя встроить в чужой iframe
#                            (кликджекинг), дублирует X-Frame-Options;
#   form-action            — формы уходят только к нам и в вход Telegram;
#   img-src ... https:     — внешние фото мастеров и аватар Telegram
#                            (см. блок про изображения);
# script/frame telegram.org — виджет входа в админку (login.html).
_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self' https://oauth.telegram.org; "
    "img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline' https://telegram.org; "
    "frame-src https://oauth.telegram.org; "
    "connect-src 'self'"
)

# Permissions-Policy: приложению не нужны камера, микрофон, геолокация,
# оплата через браузерный API и т.п. — явно выключаем, чтобы встроенный
# сторонний контент (виджет Telegram) не смог их запросить.
_PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
)


# ---------- Наблюдаемость ошибок ----------
#
# График Requests в Railway показывает долю 4xx/5xx, но не говорит, КАКОЙ
# эндпойнт их отдаёт, а искать это по логам вручную мучительно.
#
# Считаем по ШАБЛОНУ маршрута, а не по сырому пути, и это важно дважды:
#   * сырые пути раздули бы словарь без границ (сканеры дёргают случайные
#     URL сотнями);
#   * в пути живут секреты — `/club/{tenant_id}/m/{token}` содержит
#     одноразовый токен управления. Шаблон значений не содержит вовсе,
#     поэтому в счётчики и логи ничего личного не попадает.
_ERROR_COUNTS: dict[str, int] = {}
_ERROR_COUNTS_MAX = 200          # предел кардинальности


def _route_label(request: Request, status: int) -> str:
    route = request.scope.get("route")
    template = getattr(route, "path", None) or "<нет маршрута>"
    return f"{request.method} {template} -> {status}"


def _record_error(request: Request, status: int) -> None:
    key = _route_label(request, status)
    if key not in _ERROR_COUNTS and len(_ERROR_COUNTS) >= _ERROR_COUNTS_MAX:
        key = f"(прочее) -> {status}"
    _ERROR_COUNTS[key] = _ERROR_COUNTS.get(key, 0) + 1


def error_counters() -> dict[str, int]:
    """Ответы 4xx/5xx с момента старта процесса, по убыванию частоты.
    Только шаблоны маршрутов и числа — ни путей, ни секретов."""
    return dict(sorted(_ERROR_COUNTS.items(), key=lambda kv: -kv[1]))


@app.middleware("http")
async def _observe_errors(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        # Ответ (500) отдаст Starlette — нам важно, чтобы в логе остался
        # маршрут: без него traceback в Railway не связать с эндпойнтом.
        _record_error(request, 500)
        logger.exception("Необработанная ошибка: %s",
                         _route_label(request, 500))
        raise
    if response.status_code >= 400:
        _record_error(request, response.status_code)
    return response


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
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)
    # умолчание для referrer на весь сайт; страницы с личными данными
    # ужесточают его до no-referrer сами (блок manage-ссылок)
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
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
        except Exception as e:
            raise HTTPException(status_code=400, detail="bad payload") from e
        remote_ip = request.client.host if request.client else None
        async with SessionLocal() as session:
            ok = await payment_service.handle_webhook(
                session, provider, body=body, headers=dict(request.headers),
                remote_ip=remote_ip, payload=payload)
        return {"ok": ok}


@app.get("/health")
async def health(response: Response) -> dict:
    """Проверяет реальную доступность БД (SELECT 1) и читаемость ключей
    телефонов по РЕАЛЬНЫМ строкам БД — платформа (Railway) должна видеть и
    падение базы, и подменённый ключ как нездоровый контейнер, а не как
    постоянный 'ok'."""
    from sqlalchemy import text

    from app.core.version import commit_sha
    db_kind = "sqlite" if settings.is_sqlite else "postgres"
    sha = commit_sha()   # какой код реально развёрнут
    # только безопасные диагностические булевы — без адресов и значений
    proxy_ok = settings.proxy_headers_configured
    # keys_ok = конфигурация целостна И секреты подтверждены строками БД
    keys_ok = await _keys_ok()
    base = {"edition": settings.edition, "db": db_kind, "commit": sha,
            "proxy_headers_configured": proxy_ok, "keys_ok": keys_ok,
            "rss_mb": _rss_mb(),
            # Диагностика, НЕ влияющая на статус: false означает, что режим
            # доставки Telegram ещё не применён и дотягивается в фоне. Если
            # завязать на это 503, недоступность Telegram снова начала бы
            # валить деплой — ровно то, от чего мы уходим.
            "tg_delivery_synced": _tg_delivery_synced()}
    db_ok = True
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        logger.error("Health check: БД недоступна: %s", e)
        db_ok = False
    if db_ok and keys_ok:
        return {"status": "ok", **base}
    response.status_code = 503
    return {"status": "error", **base}


def _tg_delivery_synced() -> bool:
    """Применён ли режим доставки Telegram (webhook зарегистрирован либо
    снят для polling). Импорт ленивый: в Lite ботов может не быть."""
    try:
        from app.bots import telegram as _tg
        return _tg.global_delivery_synced()
    except Exception:      # noqa: BLE001 — диагностика не должна ронять /health
        return False


def _rss_mb() -> float | None:
    """Текущая резидентная память процесса, МБ. None — если платформа не
    отдаёт её дёшево (не Linux).

    Нужна именно в /health: Railway тарифицирует СРЕДНЮЮ память, и без
    возможности посмотреть RSS прямо в проде любая оптимизация памяти
    остаётся гаданием. Число безобидное — ни секретов, ни адресов."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1048576, 1)
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _keys_config_ok() -> bool:
    """Дешёвая проверка КОНФИГУРАЦИИ ключей (без скана БД): нет конфликта
    неизменяемых версий и активная версия доступна."""
    from app.core import bot_tokens, phones
    try:
        phones.assert_config_valid()
        bot_tokens.assert_config_valid()
        return True
    except Exception:
        return False


async def _verify_keys_db() -> list[str] | None:
    """Сверка ключей телефонов по РЕАЛЬНЫМ строкам БД. Возвращает список
    нечитаемых версий (пусто — всё читается), либо None, если саму сверку
    выполнить не удалось (например, БД недоступна). None трактуется вызовом
    как «не ок»: неизвестность здесь не безопаснее ошибки."""
    try:
        from app.db.engine import SessionLocal
        from app.repositories.repo import GlobalRepository
        async with SessionLocal() as s:
            return await GlobalRepository(s).verify_web_keys()
    except Exception as e:              # noqa: BLE001 — не валим вызывающего
        logger.error("КЛЮЧИ ТЕЛЕФОНОВ: сверка по данным не выполнена: %s", e)
        return None


async def _keys_ok() -> bool:
    """keys_ok для /health: конфигурация ключей целостна И их секреты
    подтверждены реальными строками БД. Дешёвый config-чек отсекает заведомо
    сломанную конфигурацию до обращения к базе; затем сверяем данные —
    подменённый ключ под существующей версией даёт False (и 503)."""
    if not _keys_config_ok():
        return False
    return await _verify_keys_db() == []   # None/непустой список → не ок


# ---------- Telegram webhook ----------

@app.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> Response:
    import hmac
    if not settings.tg_webhook_secret:
        raise HTTPException(status_code=403, detail="webhook not configured")
    if not hmac.compare_digest(
            x_telegram_bot_api_secret_token, settings.tg_webhook_secret):
        raise HTTPException(status_code=403, detail="bad secret")
    from app.bots import telegram as tg
    update = await request.json()
    await tg.feed_webhook_update(update)
    return Response(status_code=200)


@app.post("/webhook/telegram/{tenant_id}")
async def tenant_telegram_webhook(
    tenant_id: int,
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> Response:
    """Принимает update клиентского бота, фиксирует его и быстро отвечает."""
    import hmac

    from app.core import bot_tokens
    from app.db.engine import SessionLocal
    from app.models.entities import Tenant
    from app.services import inbound
    from app.services.webhook_security import (
        client_webhook_secret,
        telegram_event_id,
    )

    async with SessionLocal() as session:
        tenant = await session.get(Tenant, tenant_id)
        token = bot_tokens.token_of(tenant, "tg") if tenant else ""
    if not tenant or not tenant.is_active or \
            tenant.tg_delivery_mode != "webhook" or not token or \
            token == settings.tg_token:
        raise HTTPException(status_code=404, detail="webhook not configured")

    try:
        expected = client_webhook_secret("tg", tenant.id, token)
    except RuntimeError as exc:
        logger.error("TG webhook tenant=%s не настроен: %s", tenant_id, exc)
        raise HTTPException(status_code=503, detail="webhook unavailable") from exc
    if not hmac.compare_digest(x_telegram_bot_api_secret_token, expected):
        raise HTTPException(status_code=403, detail="bad secret")

    try:
        update = await request.json()
        event_id = telegram_event_id(update)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="bad payload") from exc
    await inbound.ingest(
        platform="tg", tenant_id=tenant.id,
        external_event_id=event_id, payload=update,
    )
    return Response(status_code=200)


# ---------- VK Callback API ----------

@app.post("/webhook/vk")
async def vk_webhook(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="bad payload") from e
    import hmac
    from sqlalchemy import select

    from app.core import bot_tokens
    from app.db.engine import SessionLocal
    from app.models.entities import Tenant
    from app.services import inbound
    from app.services.webhook_security import client_webhook_secret, vk_event_id

    try:
        group_id = int(body.get("group_id"))
    except (TypeError, ValueError):
        group_id = 0

    tenant = None
    token = ""
    if group_id:
        async with SessionLocal() as session:
            tenant = (await session.execute(
                select(Tenant).where(Tenant.vk_group_id == group_id)
            )).scalar_one_or_none()
            token = bot_tokens.token_of(tenant, "vk") if tenant else ""

    # Клиентский Callback API: у каждого клуба свой производный секрет,
    # confirmation-код и durable inbox с дедупликацией.
    if tenant is not None and token and token != settings.vk_token:
        if not tenant.is_active or tenant.vk_delivery_mode != "callback":
            raise HTTPException(status_code=404, detail="webhook not configured")
        try:
            expected = client_webhook_secret("vk", tenant.id, token)
        except RuntimeError as exc:
            logger.error("VK webhook tenant=%s не настроен: %s", tenant.id, exc)
            raise HTTPException(status_code=503, detail="webhook unavailable") from exc
        if not hmac.compare_digest(str(body.get("secret") or ""), expected):
            raise HTTPException(status_code=403, detail="bad secret")
        if body.get("type") == "confirmation":
            if not tenant.vk_confirmation_code:
                raise HTTPException(status_code=503, detail="confirmation unavailable")
            return Response(content=tenant.vk_confirmation_code,
                            media_type="text/plain")
        await inbound.ingest(
            platform="vk", tenant_id=tenant.id,
            external_event_id=vk_event_id(body), payload=body,
        )
        return Response(content="ok", media_type="text/plain")

    # Обратно совместимый глобальный Callback API.
    # Секрет обязателен: без него любой мог бы подделать событие VK Callback API.
    if not settings.vk_secret:
        logger.error("VK webhook отклонён: VK_SECRET не задан. "
                     "Укажите секрет в настройках Callback API и в VK_SECRET.")
        raise HTTPException(status_code=403, detail="webhook secret not configured")
    if not hmac.compare_digest(str(body.get("secret") or ""), settings.vk_secret):
        raise HTTPException(status_code=403, detail="bad secret")
    if body.get("type") == "confirmation":
        if not settings.vk_confirmation:
            raise HTTPException(status_code=503, detail="confirmation unavailable")
        return Response(content=settings.vk_confirmation, media_type="text/plain")
    from app.bots import vk as vk
    await vk.feed_callback_event(body)
    return Response(content="ok", media_type="text/plain")
