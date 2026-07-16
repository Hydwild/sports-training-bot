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


@router.get("/tenants/{tenant_id}/trainings", response_model=list[TrainingOut])
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
            response_model=list[SignupOut])
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

@router.post("/tenants/{tenant_id}/payments/start")
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

_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{--bg:#f4f6fa;--surface:#fff;--text:#1a1a2e;--muted:#556;
--border:#ccd;--shadow:rgba(20,30,60,.08)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14161c;--surface:#1e2129;
--text:#e7e9ee;--muted:#9aa1ad;--border:#2b2f3a;--shadow:rgba(0,0,0,.4)}}}}
*{{box-sizing:border-box}}
body{{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);
margin:0;padding:16px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:14px;
padding:16px;margin:0 auto 14px;max-width:520px;box-shadow:0 1px 4px var(--shadow)}}
h1{{font-size:22px;color:{color};text-align:center}}
.t{{font-weight:600;font-size:17px}} .m{{color:var(--muted);margin:4px 0}}
input{{width:100%;box-sizing:border-box;padding:11px;margin:6px 0;
border:1px solid var(--border);border-radius:8px;background:var(--surface);
color:var(--text);font-size:15px}}
button{{width:100%;padding:12px;border:0;border-radius:8px;background:{color};
color:#fff;font-size:16px;cursor:pointer}}
button:hover{{filter:brightness(1.07)}} .full{{color:#e0863b;font-weight:600}}
.ok{{color:#2a9d5a;font-size:18px;text-align:center}}
a{{color:{color}}}</style></head><body>
<h1>🏸 {title}</h1>{body}
<p style="text-align:center;color:var(--muted)">Запись онлайн — без регистрации</p>
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
                            body='<div class="card">⏸ Работа клуба временно '
                                 'приостановлена. Обратитесь к тренеру.</div>')
    svc = BookingService(session, tenant_id, tz=tenant.timezone)
    trainings = await svc.repo.list_upcoming()
    cards = []
    for t in trainings:
        active = await svc.repo.get_signups(t.id, "active")
        filled, mx = len(active), t.max_participants
        price = (f" · {t.price_minor // 100}₽" if getattr(t, "price_minor", 0)
                 else "")
        state = ('<span class="full">мест нет — запись в очередь</span>'
                 if filled >= mx else f"свободно: {mx - filled} из {mx}")
        names = ", ".join(_h.escape(s.name) for s in active[:12])
        who = (f'<div class="m">Записаны: {names}'
               + ("…" if len(active) > 12 else "") + "</div>") if active else ""
        cards.append(
            f'<div class="card"><div class="t">{_h.escape(t.title)}</div>'
            f'<div class="m">📅 {svc.format_local(t.start_at)}'
            f'{" · 📍 " + _h.escape(t.location) if t.location else ""}{price}</div>'
            f'<div class="m">👥 {state}</div>{who}'
            f'<form method="post" action="/club/{tenant_id}/signup">'
            f'<input type="hidden" name="training_id" value="{t.id}">'
            f'<input name="name" required minlength="2" maxlength="100" '
            f'placeholder="Ваше имя">'
            f'<input name="phone" required minlength="10" maxlength="16" '
            f'placeholder="Телефон (для тренера)">'
            f'<button>Записаться</button></form></div>')
    my_form = (f'<div class="card"><div class="t">Мои записи</div>'
               f'<form method="post" action="/club/{tenant_id}/my">'
               f'<input name="phone" required minlength="10" maxlength="16" '
               f'placeholder="Телефон, указанный при записи">'
               f'<button>Показать мои записи</button></form></div>')
    body = ("".join(cards) or ('<div class="card">Ближайших тренировок нет — '
                               'загляните позже.</div>')) + my_form
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
                            body='<div class="card">⏸ Работа клуба временно '
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
    msg = {"active": f"✅ Вы записаны, {_h.escape(name)}!",
           "queue": f"⏳ Мест нет — вы в очереди (№{res.position}). "
                    "Если место освободится, тренер свяжется по телефону.",
           "already": "Вы уже записаны на эту тренировку.",
           "closed": "Запись на эту тренировку закрыта."}.get(
               res.result, "Готово.")
    cancel_link = ""
    if res.result in ("active", "queue"):
        token = _cancel_token(tenant_id, training_id, uid)
        cancel_link = (
            f'<p style="text-align:center"><a href="/club/{tenant_id}/cancel'
            f'?t={training_id}&u={uid}&s={token}">Отменить эту запись</a></p>')
    body = (f'<div class="card"><p class="ok">{msg}</p>{cancel_link}'
            f'<p style="text-align:center"><a href="/club/{tenant_id}">'
            f'← к списку тренировок</a></p></div>')
    title = tenant.brand_name or tenant.name
    return _PAGE.format(title=_h.escape(title),
                        color=_safe_color(tenant.brand_color), body=body)


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
    await svc.cancel_signup(t, "web", u)
    await session.commit()
    body = ('<div class="card"><p class="ok">✅ Запись отменена.</p>'
            f'<p style="text-align:center"><a href="/club/{tenant_id}">'
            '← к списку тренировок</a></p></div>')
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
                    phone: str = Form(...),
                    session: AsyncSession = Depends(get_session)):
    """Мои записи по телефону: список с персональными ссылками отмены."""
    import html as _h
    tenant = await _ensure_tenant(session, tenant_id)
    from app.core.config import tenant_suspended
    if tenant_suspended(tenant):
        import html as _h2
        return _PAGE.format(title=_h2.escape(tenant.brand_name or tenant.name),
                            color="#888",
                            body='<div class="card">⏸ Работа клуба временно '
                                 'приостановлена.</div>')
    digits = "".join(c for c in phone if c.isdigit())
    if not (10 <= len(digits) <= 15):
        raise HTTPException(status_code=400, detail="Некорректный телефон")
    uid = _phone_uid(digits)
    svc = BookingService(session, tenant_id, tz=tenant.timezone)
    rows = await svc.my_trainings("web", uid)
    if not rows:
        body = ('<div class="card">По этому телефону записей не найдено.</div>'
                f'<p style="text-align:center"><a href="/club/{tenant_id}">'
                '← к списку тренировок</a></p>')
    else:
        items = []
        for t, status, position in rows:
            mark = ("✅ записаны" if status == "active"
                    else f"⏳ в очереди №{position}")
            token = _cancel_token(tenant_id, t.id, uid)
            items.append(
                f'<div class="card"><div class="t">{_h.escape(t.title)}</div>'
                f'<div class="m">📅 {svc.format_local(t.start_at)} — {mark}</div>'
                f'<p><a href="/club/{tenant_id}/cancel?t={t.id}&u={uid}'
                f'&s={token}">Отменить запись</a></p></div>')
        items.append(f'<p style="text-align:center">'
                     f'<a href="/club/{tenant_id}">← к списку</a></p>')
        body = "".join(items)
    title = tenant.brand_name or tenant.name
    return _PAGE.format(title=_h.escape(title),
                        color=_safe_color(tenant.brand_color), body=body)


@public_router.get("/promo", response_class=HTMLResponse)
async def promo_page():
    """Промо-страница продукта (лендинг). Контакты и цены — в promo_page.py."""
    from app.api.promo_page import PROMO_HTML
    return PROMO_HTML
