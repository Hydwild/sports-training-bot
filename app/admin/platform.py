"""
Панель оператора платформы: подключение новых клиентов (клубов) без ручных
curl-запросов к /api — см. MULTICLIENT.md для API-варианта того же самого.

Это НЕ роль внутри клуба (owner/coach/assistant), а доступ владельца всей
площадки. Вход — тем же секретом, что уже используется как X-Admin-Token
для служебных эндпойнтов /api (ADMIN_API_TOKEN), просто обёрнутым в форму
вместо curl. Отдельная cookie (platform_token), не пересекается с
тенант-админкой (access_token).
"""
from __future__ import annotations

import datetime as dt
import hmac
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.routes import _cookie_secure
from app.api.routes import (
    BillingPatch,
    TokensPatch,
    _rate_ok,
    client_ip,
    create_tenant as _create_tenant,
    set_tenant_billing as _set_tenant_billing,
    set_tenant_tokens as _set_tenant_tokens,
)
from app.api.schemas import TenantCreate
from app.core import bot_tokens
from app.core.config import settings, tenant_suspended
from app.core.security import NotAuthenticated, csrf_for_request, require_csrf
from app.db.engine import get_session
from app.repositories.repo import GlobalRepository

router = APIRouter(prefix="/admin/platform", tags=["platform-admin"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

COOKIE = "platform_token"


def require_platform_admin(request: Request) -> None:
    """Гейт для страниц оператора: сравнивает cookie с ADMIN_API_TOKEN —
    тем же секретом, что уже защищает /api/tenants и т.п."""
    expected = settings.admin_api_token or ""
    got = request.cookies.get(COOKIE, "")
    if not expected or not hmac.compare_digest(got, expected):
        raise NotAuthenticated("/admin/platform/login")


def _ctx(request: Request, **extra) -> dict:
    return {
        "request": request, "role": "Оператор",
        "brand_name": "Панель оператора", "brand_color": "#3a7bd5",
        "brand_logo_url": None, "logout_url": "/admin/platform/logout",
        "csrf": csrf_for_request(request, COOKIE),
        **extra,
    }


def _anon_ctx(request: Request, **extra) -> dict:
    """Контекст для страницы логина — без role (нет сессии, значит и ссылки
    «Выйти» быть не должно)."""
    return {
        "request": request, "role": None,
        "brand_name": "Панель оператора", "brand_color": "#3a7bd5",
        "brand_logo_url": None,
        **extra,
    }


# ---------- Вход ----------

@router.get("/login", response_class=HTMLResponse)
async def platform_login_page(request: Request):
    return templates.TemplateResponse(request, "platform_login.html",
        _anon_ctx(request, configured=bool(settings.admin_api_token)))


@router.post("/login")
async def platform_login_submit(request: Request, token: str = Form(...)):
    ip = client_ip(request)
    if not _rate_ok(ip, limit=5, window=300, scope="platform-login"):
        raise HTTPException(status_code=429,
                            detail="Слишком много попыток, попробуйте позже")
    expected = settings.admin_api_token or ""
    if not expected or not hmac.compare_digest(token.strip(), expected):
        return templates.TemplateResponse(request, "platform_login.html",
            _anon_ctx(request, configured=bool(expected), error="Неверный токен"),
            status_code=401)
    resp = RedirectResponse("/admin/platform", status_code=302)
    resp.set_cookie(COOKIE, token.strip(), httponly=True, samesite="lax",
                    secure=_cookie_secure())
    return resp


@router.get("/logout")
async def platform_logout():
    resp = RedirectResponse("/admin/platform/login", status_code=302)
    resp.delete_cookie(COOKIE)
    return resp


# ---------- Дашборд: список клиентов ----------

async def _dashboard_rows(request: Request, session: AsyncSession) -> list[dict]:
    g = GlobalRepository(session)
    tenants = await g.list_tenants()
    soon = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    base = settings.public_base_url or str(request.base_url).rstrip("/")

    rows = []
    for t in tenants:
        paid_until = (t.paid_until or "").strip()
        if not paid_until:
            status = ("нет ограничения", "ok")
        elif tenant_suspended(t):
            status = (f"истекла {paid_until}", "no")
        elif paid_until <= soon:
            status = (f"истекает {paid_until}", "no")
        else:
            status = (f"до {paid_until}", "ok")
        rows.append({
            "id": t.id, "name": t.brand_name or t.name, "is_demo": t.is_demo,
            "has_tg": bot_tokens.has_token(t, "tg"),
            "has_vk": bot_tokens.has_token(t, "vk"),
            "admin_tg_id": t.admin_tg_id,
            "status_text": status[0], "status_tag": status[1],
            "public_url": f"{base}/club/{t.id}",
            "edit_url": f"/admin/platform/{t.id}/edit",
        })
    return rows


@router.get("", response_class=HTMLResponse)
async def platform_dashboard(request: Request,
                             _auth: None = Depends(require_platform_admin),
                             session: AsyncSession = Depends(get_session)):
    rows = await _dashboard_rows(request, session)
    pending = await GlobalRepository(session).list_pending_reviews()
    return templates.TemplateResponse(
        request, "platform_dashboard.html",
        _ctx(request, tenants=rows, backup_msg=None,
             pending_reviews_count=len(pending)))


# ---------- Бэкап базы вручную (внешний, в Telegram) ----------

@router.post("/backup-now", response_class=HTMLResponse)
async def platform_backup_now(request: Request,
                              _auth: None = Depends(require_platform_admin),
                              _csrf: None = Depends(require_csrf(COOKIE)),
                              session: AsyncSession = Depends(get_session)):
    from app.services import backup
    result = await backup.send_backup_to_owner()
    rows = await _dashboard_rows(request, session)
    pending = await GlobalRepository(session).list_pending_reviews()
    return templates.TemplateResponse(
        request, "platform_dashboard.html",
        _ctx(request, tenants=rows, backup_msg=result,
             pending_reviews_count=len(pending)))


# ---------- Модерация отзывов (/reviews) ----------

@router.get("/reviews", response_class=HTMLResponse)
async def platform_reviews(request: Request,
                           _auth: None = Depends(require_platform_admin),
                           session: AsyncSession = Depends(get_session)):
    g = GlobalRepository(session)
    pending = await g.list_pending_reviews()
    approved = await g.list_approved_reviews(limit=200)
    return templates.TemplateResponse(
        request, "platform_reviews.html",
        _ctx(request, pending=pending, approved=approved))


@router.post("/reviews/{review_id}/approve", response_class=HTMLResponse)
async def platform_review_approve(review_id: int, request: Request,
                                  _auth: None = Depends(require_platform_admin),
                                  _csrf: None = Depends(require_csrf(COOKIE)),
                                  session: AsyncSession = Depends(get_session)):
    g = GlobalRepository(session)
    await g.set_review_approved(review_id, True)
    await session.commit()
    return RedirectResponse(url="/admin/platform/reviews", status_code=303)


@router.post("/reviews/{review_id}/delete", response_class=HTMLResponse)
async def platform_review_delete(review_id: int, request: Request,
                                 _auth: None = Depends(require_platform_admin),
                                 _csrf: None = Depends(require_csrf(COOKIE)),
                                 session: AsyncSession = Depends(get_session)):
    g = GlobalRepository(session)
    await g.delete_review(review_id)
    await session.commit()
    return RedirectResponse(url="/admin/platform/reviews", status_code=303)


# ---------- Конфигуратор бота для нового клиента ----------
# Та же сборка, что и /admin/builder (для владельца клуба), но доступна
# оператору площадки напрямую — без необходимости логиниться под кого-то
# из клубов. Логика — в app/services/bot_builder.py.

@router.get("/builder", response_class=HTMLResponse)
async def platform_builder(request: Request,
                           _auth: None = Depends(require_platform_admin)):
    return templates.TemplateResponse(request, "platform_builder.html",
                                      _ctx(request))


@router.post("/builder")
async def platform_builder_generate(
        request: Request,
        _auth: None = Depends(require_platform_admin),
        _csrf: None = Depends(require_csrf(COOKIE)),
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


# ---------- Мастера клуба (салоны/тренеры) ----------

async def _masters_ctx(request: Request, session: AsyncSession,
                       tenant_id: int, **extra) -> dict:
    from app.repositories.repo import TenantRepository
    g = GlobalRepository(session)
    tenant = await g.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Клуб не найден")
    repo = TenantRepository(session, tenant_id)
    masters = await repo.list_masters(active_only=False)
    stats = await repo.master_rating_stats()
    return _ctx(request, t=tenant, masters=masters, stats=stats,
                error=None, saved=False, **extra)


@router.get("/{tenant_id}/masters", response_class=HTMLResponse)
async def platform_masters(tenant_id: int, request: Request,
                           _auth: None = Depends(require_platform_admin),
                           session: AsyncSession = Depends(get_session)):
    ctx = await _masters_ctx(request, session, tenant_id)
    return templates.TemplateResponse(request, "platform_masters.html", ctx)


@router.post("/{tenant_id}/masters/add", response_class=HTMLResponse)
async def platform_masters_add(tenant_id: int, request: Request,
                               _auth: None = Depends(require_platform_admin),
                               _csrf: None = Depends(require_csrf(COOKIE)),
                               name: str = Form(...),
                               specialty: str = Form(""),
                               bio: str = Form(""),
                               photo_url: str = Form(""),
                               session: AsyncSession = Depends(get_session)):
    from pydantic import ValidationError
    from app.api.schemas import MasterCreate
    from app.repositories.repo import TenantRepository
    try:
        # та же валидация, что в API (в т.ч. http(s) для photo_url —
        # адрес попадает в <img src> публичной страницы)
        body = MasterCreate(name=name, specialty=specialty,
                            bio=bio, photo_url=photo_url or None)
    except ValidationError:
        ctx = await _masters_ctx(request, session, tenant_id)
        ctx["error"] = ("Проверьте поля: имя от 2 символов, фото — "
                        "http(s)-ссылка на картинку")
        return templates.TemplateResponse(
            request, "platform_masters.html", ctx, status_code=400)
    repo = TenantRepository(session, tenant_id)
    await repo.add_master(name=body.name.strip(),
                          specialty=body.specialty.strip(),
                          bio=body.bio.strip(),
                          photo_url=body.photo_url)
    await session.commit()
    return RedirectResponse(f"/admin/platform/{tenant_id}/masters",
                            status_code=303)


@router.post("/{tenant_id}/masters/{master_id}/toggle",
             response_class=HTMLResponse)
async def platform_masters_toggle(tenant_id: int, master_id: int,
                                  request: Request,
                                  _auth: None = Depends(require_platform_admin),
                                  _csrf: None = Depends(require_csrf(COOKIE)),
                                  session: AsyncSession = Depends(get_session)):
    from app.repositories.repo import TenantRepository
    repo = TenantRepository(session, tenant_id)
    m = await repo.get_master(master_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Мастер не найден")
    await repo.set_master_active(master_id, not m.active)
    await session.commit()
    return RedirectResponse(f"/admin/platform/{tenant_id}/masters",
                            status_code=303)


# ---------- Добавить клиента ----------

@router.get("/new", response_class=HTMLResponse)
async def platform_new_form(request: Request,
                            _auth: None = Depends(require_platform_admin)):
    return templates.TemplateResponse(request, "platform_new.html",
                                      _ctx(request, error=None))


@router.post("/new", response_class=HTMLResponse)
async def platform_new_submit(request: Request,
                              _auth: None = Depends(require_platform_admin),
                              _csrf: None = Depends(require_csrf(COOKIE)),
                              club_name: str = Form(...),
                              timezone: str = Form("Europe/Moscow"),
                              tg_token: str = Form(""),
                              vk_token: str = Form(""),
                              admin_tg_id: str = Form(""),
                              is_demo: str = Form(""),
                              vertical: str = Form("sport"),
                              session: AsyncSession = Depends(get_session)):
    name = club_name.strip()[:200]
    if not name:
        return templates.TemplateResponse(request, "platform_new.html",
            _ctx(request, error="Название клуба обязательно"), status_code=400)

    admin_id = int(admin_tg_id) if admin_tg_id.strip().isdigit() else None
    tenant_out = await _create_tenant(
        TenantCreate(name=name, timezone=timezone.strip() or "Europe/Moscow",
                    admin_tg_id=admin_id, is_demo=bool(is_demo),
                    vertical=vertical),
        session)

    reload_note = "Токены ботов не заданы — клуб создан, привяжите их позже."
    if tg_token.strip() or vk_token.strip():
        try:
            result = await _set_tenant_tokens(
                tenant_out.id,
                TokensPatch(tg_token=tg_token.strip() or None,
                           vk_token=vk_token.strip() or None),
                session)
            reload_note = result["note"]
        except HTTPException as e:
            # клуб уже создан — не откатываем, просто показываем ошибку токена,
            # оператор сможет донастроить с дашборда через /api вручную
            return templates.TemplateResponse(request, "platform_new.html",
                _ctx(request, error=f"Клуб «{name}» создан (id={tenant_out.id}), "
                                    f"но токен не принят: {e.detail}"),
                status_code=400)

    base = settings.public_base_url or str(request.base_url).rstrip("/")
    return templates.TemplateResponse(request, "platform_new_done.html",
        _ctx(request, tenant=tenant_out, reload_note=reload_note,
             public_url=f"{base}/club/{tenant_out.id}"))


# ---------- Изменить клиента ----------

@router.get("/{tenant_id}/edit", response_class=HTMLResponse)
async def platform_edit_form(tenant_id: int, request: Request,
                             _auth: None = Depends(require_platform_admin),
                             session: AsyncSession = Depends(get_session)):
    g = GlobalRepository(session)
    tenant = await g.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Клуб не найден")
    return templates.TemplateResponse(request, "platform_edit.html",
        _ctx(request, t=tenant, error=None, saved=False,
             tg_state=bot_tokens.mask(tenant, "tg"),
             vk_state=bot_tokens.mask(tenant, "vk")))


@router.post("/{tenant_id}/edit", response_class=HTMLResponse)
async def platform_edit_submit(tenant_id: int, request: Request,
                               _auth: None = Depends(require_platform_admin),
                               _csrf: None = Depends(require_csrf(COOKIE)),
                               club_name: str = Form(...),
                               timezone: str = Form("Europe/Moscow"),
                               tg_token: str = Form(""),
                               vk_token: str = Form(""),
                               admin_tg_id: str = Form(""),
                               admin_vk_id: str = Form(""),
                               is_demo: str = Form(""),
                               vertical: str = Form("sport"),
                               cover_url: str = Form(""),
                               about: str = Form(""),
                               address: str = Form(""),
                               contact_phone: str = Form(""),
                               session: AsyncSession = Depends(get_session)):
    g = GlobalRepository(session)
    tenant = await g.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Клуб не найден")

    name = club_name.strip()[:200]
    if not name:
        return templates.TemplateResponse(request, "platform_edit.html",
            _ctx(request, t=tenant, error="Название клуба обязательно", saved=False),
            status_code=400)

    # это форма-снимок всего клуба: пустое поле тренера/токена значит
    # "убрать значение", а не "оставить как было"
    tenant.name = name
    tenant.timezone = timezone.strip() or "Europe/Moscow"
    tenant.admin_tg_id = int(admin_tg_id) if admin_tg_id.strip().isdigit() else None
    tenant.admin_vk_id = int(admin_vk_id) if admin_vk_id.strip().isdigit() else None
    tenant.is_demo = bool(is_demo)
    from app.core.verticals import VERTICALS
    tenant.vertical = vertical if vertical in VERTICALS else "sport"
    # витрина: обложка попадает в <img src> публичной страницы —
    # принимаем только http(s), иначе показываем ошибку
    cover_url = cover_url.strip()
    if cover_url and not cover_url.startswith(("http://", "https://")):
        return templates.TemplateResponse(request, "platform_edit.html",
            _ctx(request, t=tenant, saved=False,
                 tg_state=bot_tokens.mask(tenant, "tg"),
                 vk_state=bot_tokens.mask(tenant, "vk"),
                 error="Фото-обложка должна быть http(s)-ссылкой на картинку"),
            status_code=400)
    tenant.cover_url = cover_url[:500] or None
    tenant.about = about.strip()[:2000] or None
    tenant.address = address.strip()[:300] or None
    tenant.contact_phone = contact_phone.strip()[:32] or None
    await session.commit()

    try:
        # пустое поле означает «оставить прежний токен», а не «стереть»:
        # иначе любое сохранение формы молча отвязывало бы ботов клуба.
        # Для отвязки есть отдельная кнопка (см. ниже /tokens/clear).
        patch = TokensPatch(tg_token=tg_token.strip() or None,
                            vk_token=vk_token.strip() or None)
        if patch.tg_token or patch.vk_token:
            # переиспользуем валидацию формата + hot-reload ботов
            await _set_tenant_tokens(tenant_id, patch, session)
    except HTTPException as e:
        # имя/тренер/таймзона уже сохранены — сообщаем только про токен
        return templates.TemplateResponse(request, "platform_edit.html",
            _ctx(request, t=tenant, error=f"Токен не принят: {e.detail}",
                 saved=False, tg_state=bot_tokens.mask(tenant, "tg"),
                 vk_state=bot_tokens.mask(tenant, "vk")),
            status_code=400)

    return templates.TemplateResponse(request, "platform_edit.html",
        _ctx(request, t=tenant, error=None, saved=True,
             tg_state=bot_tokens.mask(tenant, "tg"),
             vk_state=bot_tokens.mask(tenant, "vk")))


# ---------- Быстрое продление оплаты ----------

@router.post("/{tenant_id}/billing")
async def platform_billing_submit(tenant_id: int, request: Request,
                                  _auth: None = Depends(require_platform_admin),
                                  _csrf: None = Depends(require_csrf(COOKIE)),
                                  paid_until: str = Form(""),
                                  session: AsyncSession = Depends(get_session)):
    await _set_tenant_billing(tenant_id, BillingPatch(paid_until=paid_until.strip()),
                              session)
    return RedirectResponse("/admin/platform", status_code=302)


@router.post("/{tenant_id}/tokens/clear", response_class=HTMLResponse)
async def platform_tokens_clear(tenant_id: int, request: Request,
                                kind: str = Form(...),
                                _auth: None = Depends(require_platform_admin),
                                _csrf: None = Depends(require_csrf(COOKIE)),
                                session: AsyncSession = Depends(get_session)):
    """Отвязать бота от клуба — отдельным осознанным действием.

    Раньше отвязка происходила от пустого поля в форме: любое сохранение
    настроек молча выключало ботов клуба."""
    if kind not in ("tg", "vk"):
        raise HTTPException(status_code=400, detail="Неизвестный тип токена")
    g = GlobalRepository(session)
    tenant = await g.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Клуб не найден")
    bot_tokens.set_token(tenant, kind, "")
    await session.commit()
    return RedirectResponse(f"/admin/platform/{tenant_id}/edit",
                            status_code=303)
