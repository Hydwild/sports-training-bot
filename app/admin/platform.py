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
    create_tenant as _create_tenant,
    set_tenant_billing as _set_tenant_billing,
    set_tenant_tokens as _set_tenant_tokens,
)
from app.api.schemas import TenantCreate
from app.core.config import settings, tenant_suspended
from app.core.security import csrf_for_request, require_csrf
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
        raise HTTPException(status_code=401, detail="Не авторизован")


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
    ip = request.client.host if request.client else "?"
    if not _rate_ok(ip, limit=5, window=300):
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
            "id": t.id, "name": t.brand_name or t.name,
            "has_tg": bool(t.tg_token), "has_vk": bool(t.vk_token),
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
    return templates.TemplateResponse(request, "platform_dashboard.html",
                                      _ctx(request, tenants=rows, backup_msg=None))


# ---------- Бэкап базы вручную (внешний, в Telegram) ----------

@router.post("/backup-now", response_class=HTMLResponse)
async def platform_backup_now(request: Request,
                              _auth: None = Depends(require_platform_admin),
                              _csrf: None = Depends(require_csrf(COOKIE)),
                              session: AsyncSession = Depends(get_session)):
    from app.services import backup
    result = await backup.send_backup_to_owner()
    rows = await _dashboard_rows(request, session)
    return templates.TemplateResponse(request, "platform_dashboard.html",
                                      _ctx(request, tenants=rows, backup_msg=result))


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
                              session: AsyncSession = Depends(get_session)):
    name = club_name.strip()[:200]
    if not name:
        return templates.TemplateResponse(request, "platform_new.html",
            _ctx(request, error="Название клуба обязательно"), status_code=400)

    admin_id = int(admin_tg_id) if admin_tg_id.strip().isdigit() else None
    tenant_out = await _create_tenant(
        TenantCreate(name=name, timezone=timezone.strip() or "Europe/Moscow",
                    admin_tg_id=admin_id),
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
        _ctx(request, t=tenant, error=None, saved=False))


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
    await session.commit()

    try:
        # переиспользуем существующую валидацию формата + hot-reload ботов
        await _set_tenant_tokens(
            tenant_id,
            TokensPatch(tg_token=tg_token.strip(), vk_token=vk_token.strip()),
            session)
    except HTTPException as e:
        # имя/тренер/таймзона уже сохранены — сообщаем только про токен
        return templates.TemplateResponse(request, "platform_edit.html",
            _ctx(request, t=tenant, error=f"Токен не принят: {e.detail}", saved=False),
            status_code=400)

    return templates.TemplateResponse(request, "platform_edit.html",
        _ctx(request, t=tenant, error=None, saved=True))


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
