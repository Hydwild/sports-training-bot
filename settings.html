"""
HTML-админка (Jinja). Вход через Telegram Login Widget → JWT в cookie.
Роли owner/coach/assistant ограничивают действия. White-label берётся из тенанта.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    ROLE_LEVEL,
    create_token,
    current_claims,
    require_role,
    verify_telegram_auth,
)
from app.db.engine import get_session
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def _brand(session: AsyncSession, tenant_id: int) -> dict:
    g = GlobalRepository(session)
    t = await g.get_tenant(tenant_id)
    return {
        "brand_name": (t.brand_name or t.name) if t else "Badminton",
        "brand_color": t.brand_color if t else "#3a7bd5",
        "brand_logo_url": t.brand_logo_url if t else None,
    }


# ---------- Логин ----------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {
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
                    secure=bool(settings.public_base_url.startswith("https")))
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
    resp.set_cookie("access_token", token, httponly=True, samesite="lax")
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
    ctx = {"request": request, "role": claims["role"], "trainings": rows}
    ctx.update(await _brand(session, tenant_id))
    return templates.TemplateResponse("dashboard.html", ctx)


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
        return {
            "id": s.id, "name": s.name,
            "username": s.username,
            "profile_url": pl(s.username, s.user_id, platform=s.platform),
            "photo_url": s.photo_url,
            "platform": s.platform, "status": s.status,
            "attended": s.attended, "paid": s.paid,
            "is_guest": s.is_guest, "confirmed": s.confirmed,
        }

    ctx = {"request": request, "role": claims["role"],
           "t": {"id": t.id, "title": t.title, "when": svc.format_local(t.start_at),
                 "location": t.location},
           "signups": [_signup_ctx(s) for s in active + queue],
           "summary": summary, "can_edit": can_edit}
    ctx.update(await _brand(session, tenant_id))
    return templates.TemplateResponse("training.html", ctx)


# ---------- Настройки клуба (coach и выше) ----------

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request,
                        claims: dict = Depends(require_role("coach")),
                        session: AsyncSession = Depends(get_session)):
    g = GlobalRepository(session)
    tenant = await g.get_tenant(claims["tenant_id"])
    ctx = {"request": request, "role": claims["role"], "t": tenant, "saved": False}
    ctx.update(await _brand(session, claims["tenant_id"]))
    return templates.TemplateResponse("settings.html", ctx)


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request,
                        claims: dict = Depends(require_role("coach")),
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
    tenant.brand_color = (form.get("brand_color") or tenant.brand_color).strip()
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

    ctx = {"request": request, "role": claims["role"], "t": tenant, "saved": True}
    ctx.update(await _brand(session, claims["tenant_id"]))
    return templates.TemplateResponse("settings.html", ctx)


# ---------- Действия (assistant и выше) ----------

@router.post("/signups/{signup_id}/toggle_attend")
async def toggle_attend(signup_id: int,
                        claims: dict = Depends(require_role("assistant")),
                        session: AsyncSession = Depends(get_session)):
    svc = BookingService(session, claims["tenant_id"])
    s = await svc.toggle_attended(signup_id)
    if not s:
        raise HTTPException(status_code=404, detail="Не найдено")
    return RedirectResponse(f"/admin/trainings/{s.training_id}", status_code=302)


@router.post("/signups/{signup_id}/toggle_pay")
async def toggle_pay(signup_id: int,
                     claims: dict = Depends(require_role("assistant")),
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
