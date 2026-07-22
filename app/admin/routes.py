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
            # для веб-записи — суррогатный id клиента: по нему админ может
            # выдать новую ссылку управления (телефон вводить не нужно)
            "web_user_id": s.user_id if s.platform == "web" else None,
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
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502,
                            detail="Не удалось получить аватар") from e
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
                        _csrf: None = Depends(require_csrf()),
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
                        _csrf: None = Depends(require_csrf()),
                        session: AsyncSession = Depends(get_session)):
    svc = BookingService(session, claims["tenant_id"])
    s = await svc.toggle_attended(signup_id)
    if not s:
        raise HTTPException(status_code=404, detail="Не найдено")
    return RedirectResponse(f"/admin/trainings/{s.training_id}", status_code=302)


@router.post("/signups/{signup_id}/toggle_pay")
async def toggle_pay(signup_id: int,
                     claims: dict = Depends(require_role("assistant")),
                     _csrf: None = Depends(require_csrf()),
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
                           _csrf: None = Depends(require_csrf()),
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
                           yookassa_secret_key: str = Form(""),
        vertical: str = Form("sport")):
    """Собирает готовую папку бота под клиента: код + .env + база с уже
    настроенным клубом и тренером (если указан Telegram/VK ID тренера).
    Логика сборки — в app/services/bot_builder.py (общая с /admin/platform/builder)."""
    import io
    from fastapi.responses import StreamingResponse
    from app.services.bot_builder import build_bot_bundle

    zip_bytes, out_name = await build_bot_bundle(
        club_name=club_name, edition=edition, timezone=timezone,
        tg_token=tg_token, vk_token=vk_token, admin_tg_id=admin_tg_id,
        admin_vk_id=admin_vk_id, brand_name=brand_name, brand_color=brand_color,
        reminder_enabled=reminder_enabled, reminder_minutes=reminder_minutes,
        cancel_lock_minutes=cancel_lock_minutes,
        signup_close_minutes=signup_close_minutes, welcome_text=welcome_text,
        tg_bot_username=tg_bot_username, public_base_url=public_base_url,
        yookassa_shop_id=yookassa_shop_id,
        yookassa_secret_key=yookassa_secret_key, vertical=vertical)
    return StreamingResponse(
        io.BytesIO(zip_bytes), media_type="application/zip",
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


# ---------- Мастера/тренеры клуба ----------
#
# Раньше это было доступно только оператору платформы (/admin/platform):
# владелец клуба не мог сам завести мастера через веб — приходилось просить.
# Здесь та же работа, но в собственной админке клуба и в его же правах.

async def _masters_ctx(request: Request, session: AsyncSession,
                       tenant_id: int, role: str, **extra) -> dict:
    from app.core.verticals import vcfg
    from app.repositories.repo import TenantRepository

    repo = TenantRepository(session, tenant_id)
    tenant = await GlobalRepository(session).get_tenant(tenant_id)
    ctx = {
        "request": request, "role": role,
        "masters": await repo.list_masters(active_only=False),
        "stats": await repo.master_rating_stats(),
        "vc": vcfg(getattr(tenant, "vertical", None)),
        "csrf": csrf_for_request(request),
        "error": None,
    }
    ctx.update(extra)
    ctx.update(await _brand(session, tenant_id))
    return ctx


@router.get("/masters", response_class=HTMLResponse)
async def masters_page(request: Request,
                       claims: dict = Depends(require_role("coach")),
                       session: AsyncSession = Depends(get_session)):
    ctx = await _masters_ctx(request, session, claims["tenant_id"],
                             claims["role"])
    return templates.TemplateResponse(request, "masters.html", ctx)


@router.post("/masters/add", response_class=HTMLResponse)
async def masters_add(request: Request,
                      claims: dict = Depends(require_role("coach")),
                      _csrf: None = Depends(require_csrf()),
                      name: str = Form(...),
                      specialty: str = Form(""),
                      bio: str = Form(""),
                      photo_url: str = Form(""),
                      session: AsyncSession = Depends(get_session)):
    from pydantic import ValidationError

    from app.api.schemas import MasterCreate
    from app.repositories.repo import TenantRepository

    tenant_id = claims["tenant_id"]
    try:
        # та же валидация, что в API: photo_url попадает в <img src>
        # публичной страницы, поэтому только http(s)
        body = MasterCreate(name=name, specialty=specialty, bio=bio,
                            photo_url=photo_url or None)
    except ValidationError:
        ctx = await _masters_ctx(
            request, session, tenant_id, claims["role"],
            error="Проверьте поля: имя от 2 символов, фото — "
                  "http(s)-ссылка на картинку")
        return templates.TemplateResponse(request, "masters.html", ctx,
                                          status_code=400)
    repo = TenantRepository(session, tenant_id)
    await repo.add_master(name=body.name.strip(),
                          specialty=body.specialty.strip(),
                          bio=body.bio.strip(), photo_url=body.photo_url)
    await session.commit()
    return RedirectResponse("/admin/masters", status_code=303)


@router.post("/masters/{master_id}/toggle", response_class=HTMLResponse)
async def masters_toggle(master_id: int,
                         claims: dict = Depends(require_role("coach")),
                         _csrf: None = Depends(require_csrf()),
                         session: AsyncSession = Depends(get_session)):
    from app.repositories.repo import TenantRepository

    repo = TenantRepository(session, claims["tenant_id"])
    m = await repo.get_master(master_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Мастер не найден")
    await repo.set_master_active(master_id, not m.active)
    await session.commit()
    return RedirectResponse("/admin/masters", status_code=303)


# ---------- Перевыпуск ссылки управления клиенту ----------
#
# Ссылка одноразовая, а cookie-сессия живёт часы. После истечения сессии
# использованная ссылка навсегда возвращает 404, и раньше новую можно было
# получить только новой записью через сайт. Здесь администратор клуба может
# выдать новую ссылку — ПОСЛЕ того как сам сверил клиента по процедуре клуба.

import logging as _logging

_admin_logger = _logging.getLogger("app")


@router.post("/manage-link", response_class=HTMLResponse)
async def issue_manage_link_admin(request: Request,
                                  web_user_id: int = Form(...),
                                  claims: dict = Depends(require_role("coach")),
                                  _csrf: None = Depends(require_csrf()),
                                  session: AsyncSession = Depends(get_session)):
    """Выдать новую ссылку управления существующему веб-клиенту ЭТОГО клуба.

    Доступ — только авторизованному тренеру/владельцу своего tenant, с CSRF.
    Ссылка показывается ТОЛЬКО в этом ответе (no-store), в БД лежит лишь её
    SHA-256, в лог/аудит попадает факт выпуска БЕЗ токена. Выпуск отзывает
    прежние неиспользованные ссылки, но НЕ гасит уже активную сессию."""
    from app.api.routes import _issue_manage_link
    from app.repositories.repo import TenantRepository

    tenant_id = claims["tenant_id"]
    repo = TenantRepository(session, tenant_id)
    # tenant isolation: клиент должен принадлежать этому клубу
    if not await repo.customer_exists(web_user_id):
        raise HTTPException(status_code=404, detail="Клиент не найден в клубе")

    svc = BookingService(session, tenant_id)
    link = await _issue_manage_link(svc, tenant_id, web_user_id)
    await session.commit()
    # аудит БЕЗ самого токена
    _admin_logger.warning(
        "Оператор tenant=%s выдал новую ссылку управления клиенту uid=%s",
        tenant_id, web_user_id)

    ctx = {"request": request, "role": claims["role"],
           "link": link, "web_user_id": web_user_id}
    ctx.update(await _brand(session, tenant_id))
    resp = templates.TemplateResponse(request, "manage_link_issued.html", ctx)
    # ссылка не должна оседать в кешах и промежуточных прокси
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp
