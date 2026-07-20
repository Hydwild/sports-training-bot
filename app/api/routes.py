"""REST API: управление тенантами и тренировками (защищено токеном админа)."""
from __future__ import annotations

from pydantic import BaseModel
import re
from fastapi import APIRouter, Depends, Header, HTTPException, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    BrandUpdate,
    MembershipSet,
    PaymentStart,
    SignupOut,
    TenantCreate,
    TenantOut,
    TrainingCreate,
    TrainingOut,
)
from app.core.config import settings, safe_color as _safe_color
from app.db.engine import get_session
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService

router = APIRouter(prefix="/api", tags=["api"])


async def require_admin(x_admin_token: str = Header(default="")) -> None:
    """Простая защита служебных эндпойнтов общим токеном площадки.
    Сравнение через compare_digest — защита от timing-атак."""
    import hmac
    expected = settings.admin_api_token or ""
    if not expected or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="Invalid admin token")


@router.post("/tenants", response_model=TenantOut,
             dependencies=[Depends(require_admin)])
async def create_tenant(body: TenantCreate,
                        session: AsyncSession = Depends(get_session)) -> TenantOut:
    g = GlobalRepository(session)
    tenant = await g.create_tenant(**body.model_dump())
    await session.commit()
    return TenantOut.model_validate(tenant)


@router.get("/tenants", response_model=list[TenantOut],
            dependencies=[Depends(require_admin)])
async def list_tenants(session: AsyncSession = Depends(get_session)) -> list[TenantOut]:
    g = GlobalRepository(session)
    return [TenantOut.model_validate(t) for t in await g.list_tenants()]


async def _ensure_tenant(session: AsyncSession, tenant_id: int):
    g = GlobalRepository(session)
    tenant = await g.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


@router.delete("/tenants/{tenant_id}", dependencies=[Depends(require_admin)])
async def delete_tenant(tenant_id: int,
                        session: AsyncSession = Depends(get_session)):
    """
    Полностью удаляет клуб и все связанные данные (тренировки, записи,
    подписчиков, платежи и т.д.). Необратимо. Нужен для удаления дубликатов.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError, ProgrammingError
    await _ensure_tenant(session, tenant_id)  # 404 если клуба нет
    # удаляем зависимые записи в безопасном порядке. Имена таблиц — из
    # константного белого списка (не из ввода), поэтому f-строка безопасна.
    skipped = []
    for table in ("signups", "payments", "outbox", "subscribers",
                  "trainings", "memberships", "groups", "schedules"):
        try:
            # SAVEPOINT: если таблицы нет в этой редакции, откатываем только
            # этот оператор, не теряя уже удалённые строки других таблиц.
            async with session.begin_nested():
                await session.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :tid"),
                    {"tid": tenant_id})
        except (OperationalError, ProgrammingError):
            skipped.append(table)  # прочие ошибки не глушим — пробросятся выше
    await session.execute(
        text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
    await session.commit()
    return {"ok": True, "deleted_tenant_id": tenant_id, "skipped_tables": skipped}


@router.get("/tenants/{tenant_id}/trainings", response_model=list[TrainingOut],
            dependencies=[Depends(require_admin)])
async def tenant_trainings(tenant_id: int,
                           include_drafts: bool = False,
                           session: AsyncSession = Depends(get_session)):
    await _ensure_tenant(session, tenant_id)
    svc = BookingService(session, tenant_id)
    trainings = await svc.repo.list_upcoming(include_drafts=include_drafts)
    return [TrainingOut.model_validate(t) for t in trainings]


@router.post("/tenants/{tenant_id}/trainings", response_model=TrainingOut,
             dependencies=[Depends(require_admin)])
async def create_training(tenant_id: int, body: TrainingCreate,
                          session: AsyncSession = Depends(get_session)):
    tenant = await _ensure_tenant(session, tenant_id)
    svc = BookingService(session, tenant_id, tz=tenant.timezone)
    training = await svc.create_training(
        title=body.title, start_at=body.start_at, location=body.location,
        max_participants=body.max_participants, duration_min=body.duration_min,
        state=body.state, publish_at=body.publish_at,
        platform="api", user_id=0,
    )
    # цена задаётся отдельно (create_training в сервисе её не принимает)
    if body.price_minor:
        training.price_minor = body.price_minor
        training.currency = body.currency
        await session.commit()
    return TrainingOut.model_validate(training)


@router.get("/tenants/{tenant_id}/trainings/{training_id}/signups",
            response_model=list[SignupOut], dependencies=[Depends(require_admin)])
async def training_signups(tenant_id: int, training_id: int,
                           session: AsyncSession = Depends(get_session)):
    await _ensure_tenant(session, tenant_id)
    svc = BookingService(session, tenant_id)
    active = await svc.repo.get_signups(training_id, "active")
    queue = await svc.repo.get_signups(training_id, "queue")
    return [SignupOut.model_validate(s) for s in active + queue]


# ---------- Роли (owner управляет составом) ----------

@router.post("/tenants/{tenant_id}/members", dependencies=[Depends(require_admin)])
async def set_member(tenant_id: int, body: MembershipSet,
                     session: AsyncSession = Depends(get_session)):
    if body.role not in ("owner", "coach", "assistant"):
        raise HTTPException(status_code=400, detail="Неверная роль")
    await _ensure_tenant(session, tenant_id)
    svc = BookingService(session, tenant_id)
    m = await svc.repo.upsert_membership(body.tg_user_id, body.role, body.name)
    await session.commit()
    return {"id": m.id, "tg_user_id": m.tg_user_id, "role": m.role}


@router.get("/tenants/{tenant_id}/members", dependencies=[Depends(require_admin)])
async def list_members(tenant_id: int,
                       session: AsyncSession = Depends(get_session)):
    await _ensure_tenant(session, tenant_id)
    svc = BookingService(session, tenant_id)
    members = await svc.repo.list_memberships()
    return [{"tg_user_id": m.tg_user_id, "role": m.role, "name": m.name}
            for m in members]


# ---------- White-label ----------

@router.patch("/tenants/{tenant_id}/brand", dependencies=[Depends(require_admin)])
async def update_brand(tenant_id: int, body: BrandUpdate,
                       session: AsyncSession = Depends(get_session)):
    tenant = await _ensure_tenant(session, tenant_id)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(tenant, field, value)
    await session.commit()
    return {"ok": True}


@router.patch("/tenants/{tenant_id}/chat", dependencies=[Depends(require_admin)])
async def update_chat(tenant_id: int,
                      tg_chat_id: int | None = None,
                      vk_group_id: int | None = None,
                      admin_tg_id: int | None = None,
                      admin_vk_id: int | None = None,
                      session: AsyncSession = Depends(get_session)):
    """
    Привязать клуб к группе Telegram (tg_chat_id) или сообществу VK
    (vk_group_id), а также задать администратора клуба в каждой платформе
    (admin_tg_id / admin_vk_id) — админ может создавать тренировки из бота.
    """
    tenant = await _ensure_tenant(session, tenant_id)
    if tg_chat_id is not None:
        tenant.tg_chat_id = tg_chat_id
    if vk_group_id is not None:
        tenant.vk_group_id = vk_group_id
    if admin_tg_id is not None:
        tenant.admin_tg_id = admin_tg_id
    if admin_vk_id is not None:
        tenant.admin_vk_id = admin_vk_id
    await session.commit()
    return {"ok": True, "tg_chat_id": tenant.tg_chat_id,
            "vk_group_id": tenant.vk_group_id,
            "admin_tg_id": tenant.admin_tg_id,
            "admin_vk_id": tenant.admin_vk_id}


# ---------- Старт платежа ----------

@router.post("/tenants/{tenant_id}/payments/start",
             dependencies=[Depends(require_admin)])
async def start_payment(tenant_id: int, body: PaymentStart,
                        session: AsyncSession = Depends(get_session)):
    from app.core.features import features
    if not features.payments:
        raise HTTPException(status_code=403, detail="Оплаты доступны только в Pro")
    from app.services.payment_service import PaymentService
    tenant = await _ensure_tenant(session, tenant_id)
    psvc = PaymentService(session, tenant_id)
    try:
        url = await psvc.start_payment(
            training_id=body.training_id, platform=body.platform,
            user_id=body.user_id, provider_name=tenant.payment_provider,
            return_url=body.return_url)
    except (ValueError, RuntimeError, NotImplementedError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"confirmation_url": url}


class TokensPatch(BaseModel):
    tg_token: str | None = None
    vk_token: str | None = None


@router.patch("/tenants/{tenant_id}/tokens",
              dependencies=[Depends(require_admin)])
async def set_tenant_tokens(tenant_id: int, body: TokensPatch,
                            session: AsyncSession = Depends(get_session)):
    """Мультиклиент: задаёт клубу собственные токены ботов.
    После смены токенов нужен перезапуск сервиса (боты стартуют при запуске)."""
    tenant = await _ensure_tenant(session, tenant_id)
    if body.tg_token is not None:
        val = body.tg_token.strip()
        if val and not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", val):
            raise HTTPException(status_code=400,
                                detail="Неверный формат Telegram-токена")
        tenant.tg_token = val or None
    if body.vk_token is not None:
        tenant.vk_token = body.vk_token.strip() or None
    await session.commit()
    # hot-reload: пробуем поднять/перечитать ботов без рестарта сервиса
    reloaded = False
    try:
        from app.bots import telegram as _tg
        from app.bots import vk as _vk
        await _tg.reload_client_bots()
        await _vk.reload_client_bots()
        reloaded = True
    except Exception as e:
        import logging
        logging.getLogger("api").warning("Hot-reload ботов не удался: %s", e)
    note = ("Боты клиента подняты без рестарта." if reloaded else
            "Перезапустите сервис, чтобы боты клиента поднялись.")
    return {"ok": True, "tenant_id": tenant_id, "reloaded": reloaded,
            "note": note}


class BillingPatch(BaseModel):
    paid_until: str = ""            # ISO-дата "2026-08-01" или "" (без лимита)


@router.patch("/tenants/{tenant_id}/billing",
              dependencies=[Depends(require_admin)])
async def set_tenant_billing(tenant_id: int, body: BillingPatch,
                             session: AsyncSession = Depends(get_session)):
    """SaaS: до какой даты оплачен клуб. Пустая строка — без ограничений.
    После даты боты и веб-страница клуба отвечают «приостановлено»."""
    import datetime as _dt
    tenant = await _ensure_tenant(session, tenant_id)
    val = (body.paid_until or "").strip()
    if val:
        try:
            _dt.date.fromisoformat(val)
        except ValueError as e:
            raise HTTPException(status_code=400,
                                detail="Дата в формате ГГГГ-ММ-ДД") from e
    tenant.paid_until = val
    await session.commit()
    return {"ok": True, "tenant_id": tenant_id, "paid_until": val}


# ─────────── Публичная страница записи (без Telegram/ВК) ───────────
from fastapi.responses import HTMLResponse  # noqa: E402

public_router = APIRouter(tags=["public"])

# простая защита от спама: не более 5 записей в минуту с одного IP.
# Примечание: счётчик живёт в памяти процесса — этого достаточно для одного
# контейнера; при нескольких воркерах/репликах лимит станет мягче (на каждый
# процесс свой), для строгого лимита нужен общий стор (Redis).
_ip_hits: dict[str, list[float]] = {}


def _phone_uid(digits: str) -> int:
    """Стабильный числовой id по телефону (только цифры). Берём телефон целиком
    — он помещается в BigInteger, поэтому у разных телефонов разные id (в отличие
    от прежнего `% 2e9`, где номера сталкивались)."""
    return int(digits)


def _rate_ok(ip: str, limit: int = 5, window: int = 60) -> bool:
    import time
    now = time.time()
    hits = [t for t in _ip_hits.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        _ip_hits[ip] = hits
        return False
    hits.append(now)
    _ip_hits[ip] = hits
    # чистим ТОЛЬКО «протухшие» IP (без активных попыток), чтобы поток запросов
    # с чужих адресов не сбрасывал лимит всем сразу.
    if len(_ip_hits) > 5000:
        stale = [k for k, v in _ip_hits.items()
                 if not any(now - t < window for t in v)]
        for k in stale:
            del _ip_hits[k]
    return True

# Мелкие line-иконки в стиле /promo (обводка наследует цвет через CSS)
_I_CAL = ('<svg viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="15.5" '
          'rx="2"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/></svg>')
_I_PIN = ('<svg viewBox="0 0 24 24"><path d="M12 21s-6.5-5.2-6.5-10a6.5 6.5 0 '
          '0113 0C18.5 15.8 12 21 12 21z"/><circle cx="12" cy="10.5" r="2.5"/></svg>')
_I_CHECK = ('<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/>'
            '<path d="M8.5 12.5l2.5 2.5 5-5.5"/></svg>')
_I_CLOCK = ('<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/>'
            '<path d="M12 7.5V12l3 2"/></svg>')
_I_INFO = ('<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/>'
           '<path d="M12 11v5M12 8v.01"/></svg>')

# Страница записи клуба: тёплая палитра общего сайта (/promo, /faq, /reviews),
# фирменный цвет клуба ({color}) — акцент кнопок/ссылок/прогресса.
_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{--bg:#f6f5f1;--surface:#ffffff;--surface-2:#efece3;--ink:#20211d;
--muted:#65645a;--border:#e4e1d6;--accent:{color};
--shadow:0 1px 2px rgba(30,28,20,.05),0 10px 30px rgba(30,28,20,.06);
--ease:cubic-bezier(.32,.72,.33,1)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141310;--surface:#1c1b17;
--surface-2:#232019;--ink:#f1eee2;--muted:#a8a495;--border:#302c22;
--shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.4)}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px 16px 48px;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}}
:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
a,button{{transition:color .15s var(--ease),border-color .15s var(--ease),
filter .15s var(--ease),transform .15s var(--ease);
-webkit-tap-highlight-color:transparent;touch-action:manipulation}}
@media (prefers-reduced-motion:reduce){{*,*::before,*::after{{
transition:none!important;animation:none!important}}}}
.eyebrow{{display:block;text-align:center;font:700 11px/1 -apple-system,system-ui,
sans-serif;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);
margin:8px 0 12px}}
h1{{font:400 28px/1.2 Georgia,"Times New Roman",serif;letter-spacing:-.01em;
text-align:center;margin:0 0 24px;text-wrap:balance}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:16px;
padding:20px;margin:0 auto 16px;max-width:560px;box-shadow:var(--shadow)}}
.head{{display:flex;align-items:baseline;justify-content:space-between;gap:12px}}
.t{{font:600 16.5px/1.35 -apple-system,system-ui,sans-serif}}
.price{{flex-shrink:0;font:600 13px/1 -apple-system,system-ui,sans-serif;
font-variant-numeric:tabular-nums;color:var(--accent);
border:1px solid var(--accent);border-radius:999px;padding:5px 10px}}
.m{{display:flex;align-items:center;gap:8px;color:var(--muted);
font:400 14px/1.5 -apple-system,system-ui,sans-serif;margin:8px 0 0}}
.m svg{{width:15px;height:15px;stroke:var(--muted);fill:none;stroke-width:1.6;
stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}}
.cap{{margin:16px 0 0}}
.bar{{height:6px;border-radius:999px;background:var(--surface-2);
border:1px solid var(--border);overflow:hidden}}
.bar i{{display:block;height:100%;background:var(--accent);border-radius:999px}}
.cap span{{display:block;margin-top:8px;font:500 13px/1.4 -apple-system,system-ui,
sans-serif;color:var(--muted)}}
.full{{color:#b8791a;font-weight:600}}
.who{{margin-top:8px;font:400 13px/1.55 -apple-system,system-ui,sans-serif;
color:var(--muted)}}
form{{margin-top:16px;display:flex;flex-direction:column;gap:8px}}
input{{width:100%;padding:13px 16px;border:1px solid var(--border);
border-radius:12px;background:var(--surface-2);color:var(--ink);font-size:16px;
font-family:inherit;transition:border-color .15s var(--ease)}}
input:hover{{border-color:var(--accent)}}
input:focus{{outline:2px solid var(--accent);outline-offset:1px}}
button{{width:100%;padding:15px;border:0;border-radius:12px;
background:var(--accent);color:#fff;
font:600 15px/1 -apple-system,system-ui,sans-serif;cursor:pointer}}
button:hover{{filter:brightness(1.07)}}
button:active{{transform:scale(.98)}}
button.ghost{{background:transparent;color:var(--accent);
border:1px solid var(--border)}}
button.ghost:hover{{border-color:var(--accent);filter:none}}
a{{color:var(--accent)}}
.ok-icon{{display:flex;justify-content:center;margin:8px 0 12px}}
.ok-icon svg{{width:44px;height:44px;stroke:var(--accent);fill:none;
stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}}
.ok{{text-align:center;font:600 17px/1.5 -apple-system,system-ui,sans-serif;
margin:0 0 4px;text-wrap:balance}}
.links{{text-align:center;margin-top:16px;
font:400 14px/1.6 -apple-system,system-ui,sans-serif}}
.links a{{display:inline-block;padding:6px 4px}}
.chip{{flex-shrink:0;font:600 12px/1 -apple-system,system-ui,sans-serif;
padding:6px 10px;border-radius:999px;border:1px solid var(--accent);
color:var(--accent);white-space:nowrap}}
.chip.q{{border-color:#b8791a;color:#b8791a}}
.danger{{color:#b23a2e}}
.note{{text-align:center;color:var(--muted);
font:400 14.5px/1.6 -apple-system,system-ui,sans-serif}}
.foot{{text-align:center;color:var(--muted);margin-top:32px;
font:400 12.5px/1.6 -apple-system,system-ui,sans-serif}}
.foot a{{color:var(--muted)}}
</style></head><body>
<span class="eyebrow">Запись на тренировки</span>
<h1>{title}</h1>{body}
<p class="foot">Запись онлайн — без регистрации ·
<a href="/promo">платформа «Бот записи на тренировки»</a></p>
</body></html>"""


@public_router.get("/club/{tenant_id}", response_class=HTMLResponse)
async def public_club(tenant_id: int,
                      session: AsyncSession = Depends(get_session)):
    """Публичная страница клуба: список тренировок + запись по имени."""
    import html as _h
    tenant = await _ensure_tenant(session, tenant_id)
    if not tenant.is_active:
        raise HTTPException(status_code=404, detail="Клуб не найден")
    from app.core.config import tenant_suspended
    if tenant_suspended(tenant):
        import html as _h2
        return _PAGE.format(title=_h2.escape(tenant.brand_name or tenant.name),
                            color="#888",
                            body='<div class="card note">Работа клуба временно '
                                 'приостановлена. Обратитесь к тренеру.</div>')
    svc = BookingService(session, tenant_id, tz=tenant.timezone)
    trainings = await svc.repo.list_upcoming()
    cards = []
    for t in trainings:
        active = await svc.repo.get_signups(t.id, "active")
        filled, mx = len(active), t.max_participants
        pct = min(100, round(filled / mx * 100)) if mx else 100
        price = (f'<span class="price">{t.price_minor // 100} ₽</span>'
                 if getattr(t, "price_minor", 0) else "")
        state = ('<span class="full">мест нет — запись в очередь</span>'
                 if filled >= mx
                 else f"<span>свободно: {mx - filled} из {mx}</span>")
        names = ", ".join(_h.escape(s.name) for s in active[:12])
        who = (f'<div class="who">Записаны: {names}'
               + ("…" if len(active) > 12 else "") + "</div>") if active else ""
        loc = (f'<div class="m">{_I_PIN} {_h.escape(t.location)}</div>'
               if t.location else "")
        cards.append(
            f'<div class="card">'
            f'<div class="head"><div class="t">{_h.escape(t.title)}</div>{price}</div>'
            f'<div class="m">{_I_CAL} {svc.format_local(t.start_at)}</div>{loc}'
            f'<div class="cap"><div class="bar"><i style="width:{pct}%"></i></div>'
            f'{state}</div>{who}'
            f'<form method="post" action="/club/{tenant_id}/signup">'
            f'<input type="hidden" name="training_id" value="{t.id}">'
            f'<input name="name" autocomplete="name" required minlength="2" '
            f'maxlength="100" placeholder="Ваше имя" aria-label="Ваше имя">'
            f'<input name="phone" type="tel" autocomplete="tel" required '
            f'minlength="10" maxlength="16" '
            f'placeholder="Телефон (для тренера)" aria-label="Телефон">'
            f'<button>Записаться</button></form></div>')
    my_form = (f'<div class="card"><div class="t">Мои записи</div>'
               f'<form method="post" action="/club/{tenant_id}/my">'
               f'<input name="phone" type="tel" autocomplete="tel" required '
               f'minlength="10" maxlength="16" '
               f'placeholder="Телефон, указанный при записи" aria-label="Телефон">'
               f'<button class="ghost">Показать мои записи</button></form></div>')
    body = ("".join(cards) or ('<div class="card note">Ближайших тренировок '
                               'нет — загляните позже.</div>')) + my_form
    title = tenant.brand_name or tenant.name
    return _PAGE.format(title=_h.escape(title),
                        color=_safe_color(tenant.brand_color), body=body)


@public_router.post("/club/{tenant_id}/signup", response_class=HTMLResponse)
async def public_signup(tenant_id: int,
                        request: Request,
                        training_id: int = Form(...),
                        name: str = Form(...),
                        phone: str = Form(...),
                        session: AsyncSession = Depends(get_session)):
    """Запись с публичной страницы: имя + телефон (телефон видит тренер)."""
    import html as _h
    ip = (request.client.host if request.client else "?")
    if not _rate_ok(ip):
        raise HTTPException(status_code=429,
                            detail="Слишком много запросов, попробуйте через минуту")
    tenant = await _ensure_tenant(session, tenant_id)
    if not tenant.is_active:
        raise HTTPException(status_code=404, detail="Клуб не найден")
    from app.core.config import tenant_suspended
    if tenant_suspended(tenant):
        import html as _h2
        return _PAGE.format(title=_h2.escape(tenant.brand_name or tenant.name),
                            color="#888",
                            body='<div class="card note">Работа клуба временно '
                                 'приостановлена. Обратитесь к тренеру.</div>')
    name = name.strip()[:100]
    digits = "".join(c for c in phone if c.isdigit())
    if len(name) < 2 or not (10 <= len(digits) <= 15):
        raise HTTPException(status_code=400, detail="Некорректные имя или телефон")
    uid = _phone_uid(digits)                   # стабильный id по телефону
    svc = BookingService(session, tenant_id, tz=tenant.timezone)
    await svc.repo.upsert_subscriber("web", uid, name)
    await svc.repo.set_alias("web", uid, f"{name} 📱+{digits}")
    res = await svc.sign_up(training_id, "web", uid, name)
    await session.commit()
    msg = {"active": f"Вы записаны, {_h.escape(name)}!",
           "queue": f"Мест нет — вы в очереди (№{res.position}). "
                    "Если место освободится, тренер свяжется по телефону.",
           "already": "Вы уже записаны на эту тренировку.",
           "closed": "Запись на эту тренировку закрыта."}.get(
               res.result, "Готово.")
    icon = {"active": _I_CHECK, "queue": _I_CLOCK}.get(res.result, _I_INFO)
    if res.result in ("active", "queue"):
        await _notify_group_card_changed(tenant_id, training_id)
    cancel_link = ""
    if res.result in ("active", "queue"):
        token = _cancel_token(tenant_id, training_id, uid)
        cancel_link = (
            f'<a href="/club/{tenant_id}/cancel'
            f'?t={training_id}&u={uid}&s={token}">Отменить эту запись</a><br>')
    body = (f'<div class="card"><div class="ok-icon">{icon}</div>'
            f'<p class="ok">{msg}</p>'
            f'<div class="links">{cancel_link}<a href="/club/{tenant_id}">'
            f'← к списку тренировок</a></div></div>')
    title = tenant.brand_name or tenant.name
    return _PAGE.format(title=_h.escape(title),
                        color=_safe_color(tenant.brand_color), body=body)


async def _notify_group_card_changed(tenant_id: int, training_id: int) -> None:
    """Запись/отмена с публичной веб-страницы должна обновить ранее
    опубликованную карточку тренировки в TG-группе клуба (если есть) —
    иначе список записавшихся там останется устаревшим до следующего
    нажатия кнопки внутри самой группы."""
    try:
        from app.bots import telegram as tg
        await tg._refresh_group_card(tenant_id, training_id)
    except Exception:
        pass  # TG может быть не настроен/недоступен — не критично для веб-записи


def _cancel_token(tenant_id: int, training_id: int, uid: int) -> str:
    """HMAC-подпись ссылки отмены — отменить может только тот, кто получил
    ссылку при записи (или знает телефон). Ключ — jwt_secret (в проде
    обязательно случайный, см. assert_production_secrets)."""
    import hashlib
    import hmac as _hmac
    msg = f"{tenant_id}:{training_id}:{uid}".encode()
    return _hmac.new(settings.jwt_secret.encode(), msg,
                     hashlib.sha256).hexdigest()[:32]


@public_router.get("/club/{tenant_id}/cancel", response_class=HTMLResponse)
async def public_cancel(tenant_id: int, t: int, u: int, s: str,
                        session: AsyncSession = Depends(get_session)):
    """Отмена записи по персональной ссылке из подтверждения."""
    import hmac as _hmac
    import html as _h
    tenant = await _ensure_tenant(session, tenant_id)
    if not _hmac.compare_digest(s, _cancel_token(tenant_id, t, u)):
        raise HTTPException(status_code=403, detail="Неверная ссылка отмены")
    svc = BookingService(session, tenant_id, tz=tenant.timezone)
    res = await svc.cancel_signup(t, "web", u)
    await session.commit()
    if res.get("cancelled"):
        await _notify_group_card_changed(tenant_id, t)
    body = (f'<div class="card"><div class="ok-icon">{_I_CHECK}</div>'
            '<p class="ok">Запись отменена.</p>'
            f'<div class="links"><a href="/club/{tenant_id}">'
            '← к списку тренировок</a></div></div>')
    title = tenant.brand_name or tenant.name
    return _PAGE.format(title=_h.escape(title),
                        color=_safe_color(tenant.brand_color), body=body)


@public_router.get("/club/{tenant_id}/qr")
async def club_qr(tenant_id: int, request: Request,
                  session: AsyncSession = Depends(get_session)):
    """PNG QR-код со ссылкой на страницу записи клуба — для печати в зале."""
    import io
    import qrcode
    from fastapi.responses import StreamingResponse
    await _ensure_tenant(session, tenant_id)
    base = (settings.public_base_url or str(request.base_url).rstrip("/"))
    url = f"{base}/club/{tenant_id}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@public_router.post("/club/{tenant_id}/my", response_class=HTMLResponse)
async def public_my(tenant_id: int,
                    request: Request,
                    phone: str = Form(...),
                    session: AsyncSession = Depends(get_session)):
    """
    Мои записи по телефону: список с персональными ссылками отмены.
    Телефон здесь фактически работает как пароль (даёт доступ к чужим
    записям и ссылкам их отмены) — лимитируем попытки по IP, как и для
    самой записи, иначе телефон можно перебирать без ограничений.
    """
    import html as _h
    ip = (request.client.host if request.client else "?")
    if not _rate_ok(ip):
        raise HTTPException(status_code=429,
                            detail="Слишком много запросов, попробуйте через минуту")
    tenant = await _ensure_tenant(session, tenant_id)
    from app.core.config import tenant_suspended
    if tenant_suspended(tenant):
        import html as _h2
        return _PAGE.format(title=_h2.escape(tenant.brand_name or tenant.name),
                            color="#888",
                            body='<div class="card note">Работа клуба временно '
                                 'приостановлена.</div>')
    digits = "".join(c for c in phone if c.isdigit())
    if not (10 <= len(digits) <= 15):
        raise HTTPException(status_code=400, detail="Некорректный телефон")
    uid = _phone_uid(digits)
    svc = BookingService(session, tenant_id, tz=tenant.timezone)
    rows = await svc.my_trainings("web", uid)
    if not rows:
        body = ('<div class="card note">По этому телефону записей '
                'не найдено.</div>'
                f'<div class="links"><a href="/club/{tenant_id}">'
                '← к списку тренировок</a></div>')
    else:
        items = []
        for t, status, position in rows:
            chip = ('<span class="chip">записаны</span>' if status == "active"
                    else f'<span class="chip q">в очереди №{position}</span>')
            token = _cancel_token(tenant_id, t.id, uid)
            items.append(
                f'<div class="card">'
                f'<div class="head"><div class="t">{_h.escape(t.title)}</div>'
                f'{chip}</div>'
                f'<div class="m">{_I_CAL} {svc.format_local(t.start_at)}</div>'
                f'<div class="links" style="text-align:left;margin-top:12px">'
                f'<a href="/club/{tenant_id}/cancel?t={t.id}&u={uid}'
                f'&s={token}" class="danger">Отменить запись</a></div></div>')
        items.append(f'<div class="links">'
                     f'<a href="/club/{tenant_id}">← к списку</a></div>')
        body = "".join(items)
    title = tenant.brand_name or tenant.name
    return _PAGE.format(title=_h.escape(title),
                        color=_safe_color(tenant.brand_color), body=body)


@public_router.get("/promo", response_class=HTMLResponse)
async def promo_page():
    """Промо-страница продукта (лендинг). Контакты и цены — в promo_page.py."""
    from app.api.promo_page import PROMO_HTML
    return PROMO_HTML


@public_router.get("/faq", response_class=HTMLResponse)
async def faq_page():
    """FAQ для клиентов: как записаться, создать тренировку и т.д. — в faq_page.py."""
    from app.api.faq_page import FAQ_HTML
    return FAQ_HTML


@public_router.get("/reviews", response_class=HTMLResponse)
async def reviews_page(sent: str = "",
                       session: AsyncSession = Depends(get_session)):
    """Витрина отзывов + форма отправки — в reviews_page.py."""
    from app.api.reviews_page import render_reviews_page
    g = GlobalRepository(session)
    reviews = await g.list_approved_reviews()
    notice = ("Спасибо! Отзыв отправлен и появится на странице после проверки."
              if sent == "1" else None)
    return render_reviews_page(reviews, notice=notice)


@public_router.post("/reviews", response_class=HTMLResponse)
async def reviews_submit(request: Request,
                         name: str = Form(...),
                         club_name: str = Form(""),
                         rating: int = Form(...),
                         text: str = Form(...),
                         website: str = Form(""),
                         session: AsyncSession = Depends(get_session)):
    """Приём нового отзыва: honeypot-поле + лимит по IP против спама,
    отзыв уходит в модерацию (approved=False) и не виден на странице сразу."""
    from fastapi.responses import RedirectResponse
    from app.api.reviews_page import render_reviews_page
    ip = (request.client.host if request.client else "?")
    g = GlobalRepository(session)

    if website.strip():
        # honeypot заполнен — почти наверняка бот; тихо "принимаем",
        # чтобы не подсказывать боту, что его вычислили
        return RedirectResponse(url="/reviews?sent=1", status_code=303)

    if not _rate_ok(ip):
        reviews = await g.list_approved_reviews()
        return render_reviews_page(
            reviews, notice="Слишком много попыток, попробуйте через минуту.",
            notice_kind="err")

    name = name.strip()
    text = text.strip()
    if not name or not text or not (1 <= rating <= 5):
        reviews = await g.list_approved_reviews()
        return render_reviews_page(
            reviews, notice="Заполните имя, текст отзыва и оценку.",
            notice_kind="err")

    await g.add_review(name=name[:120], club_name=club_name.strip()[:160],
                       rating=rating, text=text[:1000])
    await session.commit()

    if settings.platform_owner_tg_id:
        from app.bots import telegram as tg
        try:
            await tg.send_text_to_owner(
                settings.platform_owner_tg_id,
                f"⭐ Новый отзыв на модерации: «{name}», оценка {rating}/5.\n"
                f"Проверить: /admin/platform/reviews")
        except Exception:
            pass

    return RedirectResponse(url="/reviews?sent=1", status_code=303)
