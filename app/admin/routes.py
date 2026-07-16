"""
HTML-админка (Jinja). Вход через Telegram Login Widget → JWT в cookie.
Роли owner/coach/assistant ограничивают действия. White-label берётся из тенанта.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import safe_color, settings
from app.core.security import (
    ROLE_LEVEL,
    create_token,
    csrf_for_request,
    current_claims,
    require_csrf,
    require_role,
    verify_telegram_auth,
)
from app.db.engine import get_session
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _cookie_secure() -> bool:
    """Ставить ли флаг Secure у cookie авторизации. По умолчанию — да в боевом
    режиме (https-URL или pro без dev-входа); выключается только для локальной
    отладки по http."""
    if settings.public_base_url.startswith("https"):
        return True
    return settings.is_pro and not settings.admin_dev_login


async def _brand(session: AsyncSession, tenant_id: int) -> dict:
    g = GlobalRepository(session)
    t = await g.get_tenant(tenant_id)
    # brand_color попадает прямо в <style> базового шаблона — санируем при
    # выводе (defense-in-depth поверх валидации при сохранении).
    return {
        "brand_name": (t.brand_name or t.name) if t else "Badminton",
        "brand_color": safe_color(t.brand_color) if t else "#3a7bd5",
        "brand_logo_url": t.brand_logo_url if t else None,
    }


# ---------- Логин ----------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {
        "request": request,
        "brand_name": "Badminton Platform", "brand_color": "#3a7bd5",
        "brand_logo_url": None, "role": None,
        "widget": bool(settings.tg_bot_username),
        "bot_username": settings.tg_bot_username,
        "auth_url": (settings.public_base_url or "") + "/admin/auth/telegram",
        "dev_login": settings.admin_dev_login,
    })


async def _issue_for_user(session: AsyncSession, tg_user_id: int,
                          name: str = "") -> str | None:
    """Находит роль пользователя (берём клуб с наивысшей ролью) и выдаёт JWT."""
    g = GlobalRepository(session)
    memberships = await g.list_memberships_for_user(tg_user_id)
    if not memberships:
        return None
    best = max(memberships, key=lambda m: ROLE_LEVEL.get(m.role, 0))
    return create_token(tg_user_id, best.tenant_id, best.role, name or best.name)


@router.get("/auth/telegram")
async def auth_telegram(request: Request,
                        session: AsyncSession = Depends(get_session)):
    data = dict(request.query_params)
    if not verify_telegram_auth(data):
        raise HTTPException(status_code=403, detail="Подпись Telegram неверна")
    tg_user_id = int(data["id"])
    name = (data.get("first_name", "") + " " + data.get("last_name", "")).strip()
    token = await _issue_for_user(session, tg_user_id, name)
    if not token:
        raise HTTPException(status_code=403,
                            detail="У вас нет роли ни в одном клубе")
    resp = RedirectResponse("/admin", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, samesite="lax",
                    secure=_cookie_secure())
    return resp


@router.post("/auth/dev")
async def auth_dev(tg_user_id: int = Form(...),
                   session: AsyncSession = Depends(get_session)):
    if not settings.admin_dev_login:
        raise HTTPException(status_code=404, detail="Недоступно")
    token = await _issue_for_user(session, tg_user_id)
    if not token:
        raise HTTPException(status_code=403, detail="Нет роли ни в одном клубе")
    resp = RedirectResponse("/admin", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, samesite="lax",
                    secure=_cookie_secure())
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/admin/login", status_code=302)
    resp.delete_cookie("access_token")
    return resp


# ---------- Дашборд ----------

@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, claims: dict = Depends(current_claims),
                    session: AsyncSession = Depends(get_session)):
    tenant_id = claims["tenant_id"]
    svc = BookingService(session, tenant_id)
    trainings = await svc.repo.list_upcoming(include_drafts=True)
    rows = []
    for t in trainings:
        active = await svc.repo.count_active(t.id)
        rows.append({"id": t.id, "title": t.title, "when": svc.format_local(t.start_at),
                     "active": active, "max_participants": t.max_participants,
                     "state": t.state, "price_minor": t.price_minor,
                     "currency": t.currency})
    ctx = {"request": request, "role": claims["role"], "trainings": rows,
           "tenant_id": tenant_id}
    ctx.update(await _brand(session, tenant_id))
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/trainings/{training_id}", response_class=HTMLResponse)
async def training_page(training_id: int, request: Request,
                        claims: dict = Depends(current_claims),
                        session: AsyncSession = Depends(get_session)):
    tenant_id = claims["tenant_id"]
    svc = BookingService(session, tenant_id)
    t = await svc.repo.get_training(training_id)
    if not t:
        raise HTTPException(status_code=404, detail="Не найдено")
    active = await svc.repo.get_signups(training_id, "active")
    queue = await svc.repo.get_signups(training_id, "queue")
    summary = await svc.training_attendance(training_id)
    can_edit = ROLE_LEVEL.get(claims["role"], 0) >= ROLE_LEVEL["assistant"]

    def _signup_ctx(s):
        from app.bots.user_info import profile_link as pl
        # аватар отдаём через защищённый прокси (токен бота не уходит в браузер);
        # если фото нет — прокси вернёт 404, и шаблон покажет инициалы.
        photo = (f"/admin/avatar/{s.platform}/{s.user_id}"
                 if s.platform in ("tg", "vk") else None)
        return {
            "id": s.id, "name": s.name,
            "username": s.username,
            "profile_url": pl(s.username, s.user_id, platform=s.platform),
            "photo_url": photo,
            "platform": s.platform, "status": s.status,
            "attended": s.attended, "paid": s.paid,
            "is_guest": s.is_guest, "confirmed": s.confirmed,
        }

    ctx = {"request": request, "role": claims["role"],
           "t": {"id": t.id, "title": t.title, "when": svc.format_local(t.start_at),
                 "location": t.location},
           "signups": [_signup_ctx(s) for s in active + queue],
           "summary": summary, "can_edit": can_edit,
           "csrf": csrf_for_request(request)}
    ctx.update(await _brand(session, tenant_id))
    return templates.TemplateResponse(request, "training.html", ctx)


# ---------- Прокси аватаров (токен бота не покидает сервер) ----------

# Доверенные хосты аватаров VK (публичные CDN, без секретов).
_VK_AVATAR_HOSTS = ("vk.com", "userapi.com", "vk-cdn.net", "vkuservideo.net",
                    "vkuserlive.net", "mycdn.me")


@router.get("/avatar/{platform}/{user_id}")
async def avatar_proxy(platform: str, user_id: int,
                       claims: dict = Depends(require_role("assistant")),
                       session: AsyncSession = Depends(get_session)):
    """
    Отдаёт аватар участника, скачивая его на сервере. Для Telegram токен бота
    подставляется здесь и НЕ попадает в браузер/логи. Доступ — только для ролей
    клуба и только в пределах своего тенанта.
    """
    import httpx
    from urllib.parse import urlsplit
    svc = BookingService(session, claims["tenant_id"])
    sub = await svc.repo.get_subscriber(platform, user_id)
    ref = sub.photo_url if sub else None
    if not ref:
        raise HTTPException(status_code=404, detail="Нет аватара")

    if ref.startswith("tg:"):
        from app.bots import telegram as tg
        bot = tg._bot_for(claims["tenant_id"])
        if not bot:
            raise HTTPException(status_code=404, detail="Бот недоступен")
        file_path = ref[3:].lstrip("/")
        # защита от обхода пути в file_path
        if ".." in file_path or "://" in file_path:
            raise HTTPException(status_code=400, detail="Некорректный путь")
        url = f"https://api.telegram.org/file/bot{bot.token}/{file_path}"
    elif ref.startswith("https://"):
        host = (urlsplit(ref).hostname or "").lower()
        # только публичные VK-CDN; legacy-URL с токеном Telegram не проксируем
        if not any(host == h or host.endswith("." + h)
                   for h in _VK_AVATAR_HOSTS):
            raise HTTPException(status_code=404, detail="Недоверенный источник")
        url = ref
    else:
        raise HTTPException(status_code=404, detail="Нет аватара")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Не удалось получить аватар")
    if r.status_code != 200:
        raise HTTPException(status_code=404, detail="Нет аватара")
    media = r.headers.get("content-type", "image/jpeg")
    if not media.startswith("image/"):
        raise HTTPException(status_code=415, detail="Не изображение")
    return Response(r.content, media_type=media,
                    headers={"Cache-Control": "private, max-age=300"})


# ---------- Настройки клуба (coach и выше) ----------

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request,
                        claims: dict = Depends(require_role("coach")),
                        session: AsyncSession = Depends(get_session)):
    g = GlobalRepository(session)
    tenant = await g.get_tenant(claims["tenant_id"])
    ctx = {"request": request, "role": claims["role"], "t": tenant, "saved": False,
           "csrf": csrf_for_request(request)}
    ctx.update(await _brand(session, claims["tenant_id"]))
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request,
                        claims: dict = Depends(require_role("coach")),
                        _csrf: None = Depends(require_csrf),
                        session: AsyncSession = Depends(get_session)):
    form = await request.form()
    g = GlobalRepository(session)
    tenant = await g.get_tenant(claims["tenant_id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Клуб не найден")

    def _int(name: str, default: int, lo: int = 0) -> int:
        try:
            return max(lo, int(form.get(name, default)))
        except (TypeError, ValueError):
            return default

    # брендинг
    tenant.brand_name = (form.get("brand_name") or "").strip() or None
    tenant.brand_color = safe_color(form.get("brand_color"), tenant.brand_color)
    tenant.brand_logo_url = (form.get("brand_logo_url") or "").strip() or None
    # уведомления (чекбоксы присутствуют в form только когда отмечены)
    tenant.reminder_enabled = "reminder_enabled" in form
    tenant.reminder_minutes = _int("reminder_minutes", tenant.reminder_minutes, 1)
    tenant.guest_reminder_minutes = _int("guest_reminder_minutes",
                                         tenant.guest_reminder_minutes, 0)
    tenant.guest_expire_enabled = "guest_expire_enabled" in form
    tenant.guest_expire_minutes = _int("guest_expire_minutes",
                                       tenant.guest_expire_minutes, 1)
    tenant.publish_notify_enabled = "publish_notify_enabled" in form
    tenant.cancel_lock_minutes = _int("cancel_lock_minutes",
                                      tenant.cancel_lock_minutes, 0)
    await session.commit()

    ctx = {"request": request, "role": claims["role"], "t": tenant, "saved": True,
           "csrf": csrf_for_request(request)}
    ctx.update(await _brand(session, claims["tenant_id"]))
    return templates.TemplateResponse(request, "settings.html", ctx)


# ---------- Действия (assistant и выше) ----------

@router.post("/signups/{signup_id}/toggle_attend")
async def toggle_attend(signup_id: int,
                        claims: dict = Depends(require_role("assistant")),
                        _csrf: None = Depends(require_csrf),
                        session: AsyncSession = Depends(get_session)):
    svc = BookingService(session, claims["tenant_id"])
    s = await svc.toggle_attended(signup_id)
    if not s:
        raise HTTPException(status_code=404, detail="Не найдено")
    return RedirectResponse(f"/admin/trainings/{s.training_id}", status_code=302)


@router.post("/signups/{signup_id}/toggle_pay")
async def toggle_pay(signup_id: int,
                     claims: dict = Depends(require_role("assistant")),
                     _csrf: None = Depends(require_csrf),
                     session: AsyncSession = Depends(get_session)):
    svc = BookingService(session, claims["tenant_id"])
    s = await svc.toggle_paid(signup_id)
    if not s:
        raise HTTPException(status_code=404, detail="Не найдено")
    return RedirectResponse(f"/admin/trainings/{s.training_id}", status_code=302)


# ---------- Экспорт (coach и выше) ----------

@router.get("/trainings/{training_id}/export.xlsx")
async def export_xlsx(training_id: int,
                      claims: dict = Depends(require_role("coach")),
                      session: AsyncSession = Depends(get_session)):
    from app.services import exporters
    svc = BookingService(session, claims["tenant_id"])
    data = await svc.export_rows(training_id)
    if not data:
        raise HTTPException(status_code=404, detail="Не найдено")
    t, rows = data
    content = exporters.build_xlsx(t.title, svc.format_local(t.start_at),
                                   t.location, t.max_participants, rows)
    return Response(content,
                    media_type="application/vnd.openxmlformats-officedocument"
                               ".spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             f'attachment; filename="training_{training_id}.xlsx"'})


@router.get("/trainings/{training_id}/export.pdf")
async def export_pdf(training_id: int,
                     claims: dict = Depends(require_role("coach")),
                     session: AsyncSession = Depends(get_session)):
    from app.services import exporters
    svc = BookingService(session, claims["tenant_id"])
    data = await svc.export_rows(training_id)
    if not data:
        raise HTTPException(status_code=404, detail="Не найдено")
    t, rows = data
    content = exporters.build_pdf(t.title, svc.format_local(t.start_at),
                                  t.location, t.max_participants, rows)
    return Response(content, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="training_{training_id}.pdf"'})


@router.get("/trainings/{training_id}/export.csv")
async def export_csv(training_id: int,
                     claims: dict = Depends(require_role("coach")),
                     session: AsyncSession = Depends(get_session)):
    svc = BookingService(session, claims["tenant_id"])
    csv_text = await svc.export_training_csv(training_id)
    if not csv_text:
        raise HTTPException(status_code=404, detail="Не найдено")
    return Response(csv_text.encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="training_{training_id}.csv"'})


# ─────────── Конфигуратор бота для клиента ───────────

@router.get("/builder", response_class=HTMLResponse)
async def builder_page(request: Request,
                       claims: dict = Depends(require_role("owner")),
                       session: AsyncSession = Depends(get_session)):
    ctx = {"request": request, "role": claims["role"],
           "csrf": csrf_for_request(request)}
    ctx.update(await _brand(session, claims["tenant_id"]))
    return templates.TemplateResponse(request, "builder.html", ctx)


@router.post("/builder")
async def builder_generate(request: Request,
                           claims: dict = Depends(require_role("owner")),
                           _csrf: None = Depends(require_csrf),
                           club_name: str = Form(...),
                           edition: str = Form("lite"),
                           timezone: str = Form("Europe/Moscow"),
                           tg_token: str = Form(...),
                           vk_token: str = Form(""),
                           admin_tg_id: str = Form(""),
                           admin_vk_id: str = Form(""),
                           brand_name: str = Form(""),
                           brand_color: str = Form("#3a7bd5"),
                           reminder_enabled: str = Form(""),
                           reminder_minutes: str = Form("60"),
                           cancel_lock_minutes: str = Form("0"),
                           signup_close_minutes: str = Form("0"),
                           welcome_text: str = Form(""),
                           tg_bot_username: str = Form(""),
                           public_base_url: str = Form(""),
                           yookassa_shop_id: str = Form(""),
                           yookassa_secret_key: str = Form("")):
    """Собирает готовую папку бота под клиента: код + .env + база с уже
    настроенным клубом и тренером (если указан Telegram/VK ID тренера)."""
    import io
    import secrets
    import zipfile
    from pathlib import Path
    from fastapi.responses import StreamingResponse

    root = Path(__file__).resolve().parents[2]
    edition = "pro" if edition == "pro" else "lite"
    vk_token = vk_token.strip()
    tz = timezone.strip() or "Europe/Moscow"
    name = club_name.strip()[:100]

    def _int_or_none(s: str) -> int | None:
        s = s.strip()
        return int(s) if s.isdigit() else None

    admin_tg = _int_or_none(admin_tg_id)
    admin_vk = _int_or_none(admin_vk_id)
    rem_minutes = _int_or_none(reminder_minutes) or 60
    lock_minutes = _int_or_none(cancel_lock_minutes) or 0
    close_minutes = _int_or_none(signup_close_minutes) or 0

    env_lines = [
        f"# Бот для клуба: {name}",
        f"EDITION={edition}",
        "DATABASE_URL=sqlite+aiosqlite:////data/badminton.db",
        f"TG_TOKEN={tg_token.strip()}",
        "TG_MODE=polling",
        f"VK_TOKEN={vk_token}",
        f"RUN_VK_POLLING={'true' if vk_token else 'false'}",
        f"JWT_SECRET={secrets.token_urlsafe(24)}",
        f"ADMIN_API_TOKEN={secrets.token_urlsafe(24)}",
        f"TIMEZONE={tz}",
        "LOG_DIR=/data/logs",
        "PORT=8080",
    ]
    if edition == "pro":
        if tg_bot_username.strip():
            env_lines.append(f"TG_BOT_USERNAME={tg_bot_username.strip()}")
        if public_base_url.strip():
            env_lines.append(f"PUBLIC_BASE_URL={public_base_url.strip()}")
        if yookassa_shop_id.strip():
            env_lines.append(f"YOOKASSA_SHOP_ID={yookassa_shop_id.strip()}")
        if yookassa_secret_key.strip():
            env_lines.append(f"YOOKASSA_SECRET_KEY={yookassa_secret_key.strip()}")
    env_text = "\n".join(env_lines) + "\n"

    # ─── seed-база: клуб и тренер настроены заранее ───
    seed_bytes = None
    if admin_tg or admin_vk:
        import tempfile
        import os as _os
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.models.entities import Base, Tenant

        _fd, tmp_path = tempfile.mkstemp(suffix=".db")
        _os.close(_fd)
        tmp_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}")
        try:
            async with tmp_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            TmpSession = async_sessionmaker(tmp_engine, expire_on_commit=False)
            async with TmpSession() as tmp_session:
                tenant = Tenant(
                    name=name, timezone=tz,
                    admin_tg_id=admin_tg, admin_vk_id=admin_vk,
                    brand_name=brand_name.strip()[:200] or name,
                    brand_color=brand_color.strip() or "#3a7bd5",
                    reminder_enabled=bool(reminder_enabled),
                    reminder_minutes=rem_minutes,
                    cancel_lock_minutes=lock_minutes,
                    signup_close_minutes=close_minutes,
                    welcome_text=welcome_text.strip()[:1000] or None,
                )
                tmp_session.add(tenant)
                await tmp_session.commit()
            await tmp_engine.dispose()
            with open(tmp_path, "rb") as f:
                seed_bytes = f.read()
        finally:
            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)

    onboarding = (
        "5. Тренер сразу увидит меню управления — клуб и права уже настроены\n"
        if seed_bytes else
        "5. Клуб не привязан к тренеру автоматически (ID не был указан) — "
        "создайте клуб и роль через Swagger (/docs), как описано в "
        "DEPLOY_CLIENT.md\n"
    )
    setup_md = (
        f"# Бот для клуба «{name}»\n\n"
        "Готовая сборка. Развёртывание на Railway:\n\n"
        "1. Залейте эту папку в новый GitHub-репозиторий\n"
        "2. Railway → New Project → Deploy from GitHub repo\n"
        "3. Settings → Volumes → Add Volume, mount path: /data\n"
        "4. Variables → Raw Editor → вставьте содержимое файла .env\n"
        f"{onboarding}"
        "6. Напишите боту /start в Telegram (и сообществу в ВК, если указано)\n\n"
        "Подробная инструкция — в DEPLOY_CLIENT.md\n"
    )

    include_files = ["Dockerfile", "start.sh", "requirements.txt",
                     "README.md", "DEPLOY_CLIENT.md", "alembic.ini"]
    include_dirs = ["app", "alembic", "migrations", "tests"]
    skip = ("__pycache__", ".pytest_cache", "logs")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname_ in include_files:
            p = root / fname_
            if p.is_file():
                zf.write(p, fname_)
        for d in include_dirs:
            base = root / d
            if not base.is_dir():
                continue
            for p in base.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(root)
                if any(part in skip for part in rel.parts):
                    continue
                if p.suffix in (".db", ".pyc") or p.name == ".env":
                    continue
                zf.write(p, str(rel))
        zf.writestr(".env", env_text)
        zf.writestr("SETUP.md", setup_md)
        if seed_bytes:
            zf.writestr("seed.db", seed_bytes)
    buf.seek(0)

    safe = "".join(c for c in club_name
                   if c.isascii() and (c.isalnum() or c in "-_ "))
    out_name = f"bot_{safe.strip().replace(' ', '_') or 'client'}.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'})


# ─────────── Резервное копирование базы ───────────

def _sqlite_path() -> str | None:
    """Путь к файлу SQLite из DATABASE_URL, либо None (если Postgres)."""
    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    # sqlite+aiosqlite:////data/badminton.db -> /data/badminton.db
    tail = url.split("///")[-1]
    return "/" + tail if url.count("/") >= 4 and not tail.startswith("/") else tail


@router.get("/backup")
async def download_backup(claims: dict = Depends(require_role("owner"))):
    """Скачать резервную копию базы (только SQLite, только владелец)."""
    import sqlite3
    import tempfile
    import datetime as _dt
    from fastapi.responses import FileResponse

    path = _sqlite_path()
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=400,
                            detail="Бэкап доступен только для SQLite-базы.")
    # безопасная горячая копия через backup API (консистентно при записи)
    _fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    src = sqlite3.connect(path)
    dst = sqlite3.connect(tmp)
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    from starlette.background import BackgroundTask
    return FileResponse(tmp, media_type="application/octet-stream",
                        filename=f"backup_{stamp}.db",
                        background=BackgroundTask(os.remove, tmp))
