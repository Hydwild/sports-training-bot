"""REST API: управление тенантами и тренировками (защищено токеном админа)."""
from __future__ import annotations

import logging

from pydantic import BaseModel
import re
from fastapi import APIRouter, Depends, Header, HTTPException, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    BrandUpdate,
    MasterCreate,
    MasterOut,
    MembershipSet,
    PaymentStart,
    SignupOut,
    TenantCreate,
    TenantOut,
    TrainingCreate,
    TrainingOut,
)
from app.core import bot_tokens as _bot_tokens
from app.core.config import settings, safe_color as _safe_color
from app.core.verticals import vcfg as _vcfg
from app.db.engine import get_session
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService

logger = logging.getLogger("app")

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
    # цена/мастер задаются отдельно (create_training в сервисе их не принимает)
    extra_commit = False
    if body.price_minor:
        training.price_minor = body.price_minor
        training.currency = body.currency
        extra_commit = True
    if body.master_id is not None:
        if await svc.repo.get_master(body.master_id) is None:
            raise HTTPException(status_code=400, detail="Мастер не найден")
        training.master_id = body.master_id
        extra_commit = True
    if extra_commit:
        await session.commit()
    return TrainingOut.model_validate(training)


# ---------- Мастера (салоны/тренеры) ----------

@router.get("/tenants/{tenant_id}/masters", response_model=list[MasterOut],
            dependencies=[Depends(require_admin)])
async def list_masters(tenant_id: int,
                       session: AsyncSession = Depends(get_session)):
    await _ensure_tenant(session, tenant_id)
    svc = BookingService(session, tenant_id)
    return [MasterOut.model_validate(m)
            for m in await svc.repo.list_masters(active_only=False)]


@router.post("/tenants/{tenant_id}/masters", response_model=MasterOut,
             dependencies=[Depends(require_admin)])
async def create_master(tenant_id: int, body: MasterCreate,
                        session: AsyncSession = Depends(get_session)):
    await _ensure_tenant(session, tenant_id)
    svc = BookingService(session, tenant_id)
    m = await svc.repo.add_master(name=body.name.strip(),
                                  specialty=body.specialty.strip(),
                                  bio=body.bio.strip(),
                                  photo_url=body.photo_url)
    await session.commit()
    return MasterOut.model_validate(m)


@router.delete("/tenants/{tenant_id}/masters/{master_id}",
               dependencies=[Depends(require_admin)])
async def delete_master(tenant_id: int, master_id: int,
                        session: AsyncSession = Depends(get_session)):
    """Скрывает мастера (active=False) — история слотов сохраняется."""
    await _ensure_tenant(session, tenant_id)
    svc = BookingService(session, tenant_id)
    if not await svc.repo.deactivate_master(master_id):
        raise HTTPException(status_code=404, detail="Мастер не найден")
    await session.commit()
    return {"ok": True}


@router.delete("/tenants/{tenant_id}/master-reviews/{review_id}",
               dependencies=[Depends(require_admin)])
async def delete_master_review(tenant_id: int, review_id: int,
                               session: AsyncSession = Depends(get_session)):
    """Удаляет некорректную оценку мастера (зачистка спама оператором)."""
    await _ensure_tenant(session, tenant_id)
    svc = BookingService(session, tenant_id)
    if not await svc.repo.delete_master_review(review_id):
        raise HTTPException(status_code=404, detail="Оценка не найдена")
    await session.commit()
    return {"ok": True}


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


class DeliveryModePatch(BaseModel):
    mode: str


@router.patch("/tenants/{tenant_id}/tokens",
              dependencies=[Depends(require_admin)])
async def set_tenant_tokens(tenant_id: int, body: TokensPatch,
                            session: AsyncSession = Depends(get_session)):
    """Мультиклиент: задаёт клубу собственные токены ботов.
    Реестры обновляются без рестарта; webhook-токен меняют только после
    возврата в polling/Long Poll, чтобы внешний секрет не устарел."""
    tenant = await _ensure_tenant(session, tenant_id)
    if body.tg_token is not None:
        val = body.tg_token.strip()
        if val and not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", val):
            raise HTTPException(status_code=400,
                                detail="Неверный формат Telegram-токена")
        current = _bot_tokens.token_of(tenant, "tg")
        if current and val != current and tenant.tg_delivery_mode == "webhook":
            raise HTTPException(
                status_code=409,
                detail="Сначала верните Telegram в polling, затем замените токен",
            )
        # пишем зашифрованным: открытым текстом токен попадал в каждый
        # дамп базы, а дамп уходит в Telegram
        _bot_tokens.set_token(tenant, "tg", val)
    if body.vk_token is not None:
        val = body.vk_token.strip()
        current = _bot_tokens.token_of(tenant, "vk")
        if current and val != current and tenant.vk_delivery_mode == "callback":
            raise HTTPException(
                status_code=409,
                detail="Сначала верните VK в Long Poll, затем замените токен",
            )
        _bot_tokens.set_token(tenant, "vk", val)
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


@router.put("/tenants/{tenant_id}/delivery/{platform}",
            dependencies=[Depends(require_admin)])
async def set_tenant_delivery_mode(
    tenant_id: int,
    platform: str,
    body: DeliveryModePatch,
    session: AsyncSession = Depends(get_session),
):
    """Регистрирует/удаляет внешний webhook и только затем меняет режим."""
    tenant = await _ensure_tenant(session, tenant_id)
    from app.services.delivery_modes import set_telegram_mode, set_vk_mode
    try:
        if platform == "tg":
            result = await set_telegram_mode(session, tenant, body.mode)
        elif platform == "vk":
            result = await set_vk_mode(session, tenant, body.mode)
        else:
            raise HTTPException(status_code=404, detail="Неизвестная платформа")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Не удалось переключить %s tenant=%s", platform, tenant_id)
        raise HTTPException(
            status_code=502,
            detail=f"API {platform.upper()} не подтвердил переключение",
        ) from exc
    return {"ok": True, "tenant_id": tenant_id, **result}


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


def _eyebrow(tenant) -> str:
    return _vcfg(getattr(tenant, "vertical", None))["web_eyebrow"]


def _plural_ratings(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "оценка"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "оценки"
    return "оценок"


def _phone_uid(digits: str) -> int:
    """УСТАРЕЛО: id, равный самому номеру телефона.

    Оставлено только для чтения исторических данных, которые ещё не прошли
    миграцию a17f6b9c4d20. Новый код обязан брать id через
    repo.web_customer_id / find_web_customer_id — номер там хранится
    зашифрованным и в записях не фигурирует."""
    return int(digits)


def client_ip(request: Request) -> str:
    """Адрес клиента для лимитов.

    Разбирать X-Forwarded-For здесь мы больше НЕ будем. Заголовок ставит
    кто угодно: клиент, обратившийся к приложению напрямую, присылал
    `X-Forwarded-For: любой.адрес` и получал новую «личность» на каждый
    запрос — лимит обходился одной строкой.

    Доверие к заголовку — вопрос развёртывания, а не обработчика: uvicorn
    запускается с --proxy-headers и --forwarded-allow-ips (см. start.sh и
    TRUSTED_PROXIES) и сам подставляет реальный адрес в client.host. Если
    прокси доверенным не объявлен, здесь окажется адрес прокси: лимит
    станет строже, но обойти его нельзя.

    Значение проверяем через ipaddress — в client.host бывает и IPv6, и
    мусор, а ключ лимита не должен расти бесконтрольно.
    """
    import ipaddress

    raw = ((request.client.host if request.client else "") or "").strip()[:64]
    if not raw:
        return "?"
    try:
        return ipaddress.ip_address(raw).compressed
    except ValueError:
        return raw[:45]      # не адрес (тестовый клиент, unix-сокет)


async def _rate_guard(session, ip: str, *, scope: str,
                      tenant_id: int | None = None,
                      limit: int = 5, window: int = 60) -> None:
    """Проверка лимита для публичных форм. Отказ — 429 с Retry-After:
    клиент должен знать, когда повторять, а не долбиться вслепую.

    Счётчик общий на все процессы приложения (PostgreSQL); на SQLite
    остаётся счётчик в памяти — см. app/api/rate_limit.py."""
    from app.api import rate_limit

    ok, retry = await rate_limit.allow(
        session, scope=scope, tenant_id=tenant_id, client=ip,
        limit=limit, window=window)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail="Слишком много запросов, попробуйте позже",
            headers={"Retry-After": str(retry)})


def _rate_ok(ip: str, limit: int = 5, window: int = 60,
             scope: str = "default", tenant_id: int | None = None) -> bool:
    """Синхронная проверка для мест без сессии БД (вход в панель
    оператора). Счёт в памяти процесса: вход не публичная форма и не
    размазан по клубам, общий счётчик ради него не нужен."""
    from app.api import rate_limit

    ok, _retry = rate_limit.check_memory(
        rate_limit.bucket_key(scope, tenant_id, ip), limit, window)
    return ok

import datetime as _dt

_RU_DOW = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_RU_MON = ["янв", "фев", "мар", "апр", "мая", "июн",
           "июл", "авг", "сен", "окт", "ноя", "дек"]

# Экраны страницы записи (воронка в стиле YClients): главное меню →
# «мастера» (чипы ближайших свободных окон) / «дата и время» (лента дней),
# фильтр слотов по дню и мастеру. Прогрессивное улучшение: без JS все
# секции видны подряд, форма записи работает как обычно.
_CLUB_JS = """<script>
(function(){
  var home = document.getElementById('scr-home');
  var mastersScr = document.getElementById('scr-masters');
  var slotsScr = document.getElementById('scr-slots');
  var list = document.getElementById('list');
  var days = Array.prototype.slice.call(document.querySelectorAll('.day'));
  var fchip = document.getElementById('mfilter');
  var curDay = days.length ? days[0].dataset.day : null;
  var mFilter = null, mName = '';

  function apply(){
    days.forEach(function(b){
      var on = b.dataset.day === curDay;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    if (list) list.querySelectorAll('.card[data-day]').forEach(function(c){
      var okDay = !days.length || c.dataset.day === curDay;
      var okM = !mFilter || c.dataset.master === mFilter;
      c.classList.toggle('show', okDay && okM);
    });
    if (fchip){
      fchip.style.display = mFilter ? 'inline-flex' : 'none';
      if (mFilter) fchip.textContent = 'Мастер: ' + mName + ' ✕';
    }
  }
  function show(scr){
    [home, mastersScr, slotsScr].forEach(function(s){
      if (s) s.classList.toggle('on', s === scr);
    });
    window.scrollTo(0, 0);
  }
  if (list) list.classList.add('js');
  days.forEach(function(b){
    b.addEventListener('click', function(){ curDay = b.dataset.day; apply(); });
  });
  if (home){
    document.body.classList.add('scr-mode');
    document.querySelectorAll('[data-nav]').forEach(function(b){
      b.addEventListener('click', function(){
        show(b.dataset.nav === 'masters' ? mastersScr : slotsScr);
      });
    });
    document.querySelectorAll('[data-back]').forEach(function(b){
      b.addEventListener('click', function(){
        mFilter = null; apply(); show(home);
      });
    });
    document.querySelectorAll('.tchip[data-slot]').forEach(function(b){
      b.addEventListener('click', function(){
        mFilter = b.dataset.m; mName = b.dataset.mname;
        if (b.dataset.day) curDay = b.dataset.day;
        apply(); show(slotsScr);
        var el = document.getElementById('slot-' + b.dataset.slot);
        if (el) setTimeout(function(){
          el.scrollIntoView({behavior: 'smooth', block: 'start'});
        }, 60);
      });
    });
    document.querySelectorAll('.tchip[data-all-of]').forEach(function(b){
      b.addEventListener('click', function(){
        mFilter = b.dataset.allOf; mName = b.dataset.mname;
        if (b.dataset.firstDay) curDay = b.dataset.firstDay;
        apply(); show(slotsScr);
      });
    });
    if (fchip) fchip.addEventListener('click', function(){
      mFilter = null; apply();
    });
    show(home);
  }
  apply();
})();
</script>"""

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
_I_PHONE = ('<svg viewBox="0 0 24 24"><path d="M5 4h4l2 5-2.5 1.5a12 12 0 '
            '005 5L15 13l5 2v4a2 2 0 01-2 2A16 16 0 013 6a2 2 0 012-2z"/></svg>')
_I_USERS = ('<svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.2"/>'
            '<path d="M3.5 19.5c0-3 2.5-5 5.5-5s5.5 2 5.5 5"/>'
            '<circle cx="16.5" cy="9" r="2.4"/>'
            '<path d="M16 14.7c2.6.3 4.5 2 4.5 4.5"/></svg>')

# Согласие на обработку — там, где посетитель ОСТАВЛЯЕТ данные. На форме
# «Мои записи» галочки нет намеренно: там человек ищет свою же запись по
# телефону, новых данных не появляется.
from app.api.public_style import consent_field as _consent_field
from app.api.public_style import consent_text as _consent_text

_CONSENT_SIGNUP = _consent_field("имени и телефона для записи")
_CONSENT_RATE = _consent_field("имени и текста отзыва для оценки мастера")

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
.days{{display:flex;gap:8px;overflow-x:auto;padding:4px 2px 12px;
margin:0 auto 8px;max-width:560px;scrollbar-width:none;
-webkit-overflow-scrolling:touch}}
.days::-webkit-scrollbar{{display:none}}
.day{{flex:0 0 auto;min-width:56px;display:flex;flex-direction:column;
align-items:center;gap:2px;padding:10px 8px;border-radius:14px;
border:1px solid var(--border);background:var(--surface);cursor:pointer;
font-family:inherit;
transition:border-color .15s var(--ease),background-color .15s var(--ease)}}
.day .dow{{font:600 10.5px/1 -apple-system,system-ui,sans-serif;
text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}}
.day .num{{font:600 17px/1.2 -apple-system,system-ui,sans-serif;
font-variant-numeric:tabular-nums;color:var(--ink)}}
.day .mon{{font:500 10.5px/1 -apple-system,system-ui,sans-serif;
color:var(--muted)}}
.day.on{{background:var(--accent);border-color:var(--accent)}}
.day.on .dow,.day.on .num,.day.on .mon{{color:#fff}}
.list.js .card[data-day]{{display:none}}
.list.js .card[data-day].show{{display:block}}
.master{{display:flex;align-items:center;gap:10px;margin-top:12px}}
.master img,.master .mi{{width:40px;height:40px;border-radius:50%;
object-fit:cover;flex-shrink:0}}
.master .mi{{display:flex;align-items:center;justify-content:center;
background:var(--surface-2);border:1px solid var(--border);
font:600 15px/1 -apple-system,system-ui,sans-serif;color:var(--accent)}}
.master b{{display:block;font:600 13.5px/1.3 -apple-system,system-ui,sans-serif}}
.master span{{display:block;font:400 12px/1.35 -apple-system,system-ui,sans-serif;
color:var(--muted)}}
.free-one{{color:var(--accent)}}
.cover{{max-width:560px;margin:0 auto 20px}}
.cover img{{display:block;width:100%;height:180px;object-fit:cover;
border-radius:16px;box-shadow:var(--shadow)}}
.about{{max-width:520px;margin:-8px auto 16px;text-align:center;
color:var(--muted);font:400 14.5px/1.6 -apple-system,system-ui,sans-serif}}
.biz-info{{display:flex;justify-content:center;align-items:center;gap:20px;
flex-wrap:wrap;max-width:560px;margin:0 auto 24px}}
.biz-info .m{{margin:0}}
.biz-info a{{text-decoration:none}}
.ms-strip{{display:flex;gap:16px;overflow-x:auto;max-width:560px;
margin:0 auto 24px;padding:4px 2px;justify-content:safe center;
scrollbar-width:none}}
.ms-strip::-webkit-scrollbar{{display:none}}
.ms-item{{flex:0 0 auto;display:flex;flex-direction:column;align-items:center;
gap:6px;min-width:68px;max-width:88px}}
.ms-item img,.ms-item .mi{{width:56px;height:56px;border-radius:50%;
object-fit:cover}}
.ms-item .mi{{display:flex;align-items:center;justify-content:center;
background:var(--surface-2);border:1px solid var(--border);
font:600 19px/1 -apple-system,system-ui,sans-serif;color:var(--accent)}}
.ms-item b{{font:600 12px/1.3 -apple-system,system-ui,sans-serif;
text-align:center}}
.ms-item span{{font:400 10.5px/1.3 -apple-system,system-ui,sans-serif;
color:var(--muted);text-align:center}}
body.scr-mode .scr{{display:none}}
body.scr-mode .scr.on{{display:block}}
.menu{{max-width:560px;margin:0 auto 16px;background:var(--surface);
border:1px solid var(--border);border-radius:16px;box-shadow:var(--shadow);
overflow:hidden}}
.menu-row{{display:flex;align-items:center;gap:14px;width:100%;padding:16px;
background:none;border:0;border-bottom:1px solid var(--border);cursor:pointer;
font:600 15px/1.3 -apple-system,system-ui,sans-serif;color:var(--ink);
text-align:left;font-family:inherit;
transition:background-color .15s var(--ease)}}
.menu-row:hover{{background:var(--surface-2)}}
.menu-row:last-child{{border-bottom:0}}
.menu-row .ic{{width:40px;height:40px;border-radius:50%;
background:var(--surface-2);display:flex;align-items:center;
justify-content:center;flex-shrink:0}}
.menu-row .ic svg{{width:18px;height:18px;stroke:var(--accent);fill:none;
stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}}
.menu-row::after{{content:"";width:8px;height:8px;flex-shrink:0;
border-right:2px solid var(--muted);border-top:2px solid var(--muted);
transform:rotate(45deg);margin-left:auto}}
.backrow{{max-width:560px;margin:0 auto 16px;display:flex;align-items:center;
gap:12px}}
.backbtn{{width:44px;height:44px;flex-shrink:0;border-radius:50%;
border:1px solid var(--border);background:var(--surface);cursor:pointer;
font-size:17px;color:var(--ink);padding:0}}
.backbtn:hover{{border-color:var(--accent)}}
.scr-title{{font:600 17px/1.3 -apple-system,system-ui,sans-serif}}
.mcard .chips{{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}}
.tchip{{padding:14px;min-height:44px;border-radius:10px;
border:1px solid var(--border);
background:var(--surface-2);font:600 13px/1 -apple-system,system-ui,sans-serif;
color:var(--ink);cursor:pointer;font-family:inherit;
transition:border-color .15s var(--ease)}}
.tchip:hover{{border-color:var(--accent)}}
.tchip.more{{color:var(--accent);background:transparent}}
#mfilter{{display:none;align-items:center;min-height:44px;margin-left:auto;
padding:12px 14px;
border-radius:999px;border:1px solid var(--accent);color:var(--accent);
background:none;font:600 12.5px/1 -apple-system,system-ui,sans-serif;
cursor:pointer;font-family:inherit}}
.mnone{{margin-top:12px;font:400 13px/1.5 -apple-system,system-ui,sans-serif;
color:var(--muted)}}
.mrate{{display:block;margin-top:3px;font:600 12.5px/1.3 -apple-system,
system-ui,sans-serif;color:var(--accent);font-variant-numeric:tabular-nums}}
.rstars{{color:var(--accent);letter-spacing:1px;font-size:12px}}
details.mrev{{margin-top:12px;border-top:1px solid var(--border);
padding-top:12px}}
details.mrev summary{{cursor:pointer;list-style:none;font:600 13px/1.4
-apple-system,system-ui,sans-serif;color:var(--accent);
display:flex;align-items:center;min-height:44px;
-webkit-tap-highlight-color:transparent}}
details.mrev summary::-webkit-details-marker{{display:none}}
.rev{{margin-top:12px}}
.rev b{{font:600 13px/1.3 -apple-system,system-ui,sans-serif}}
.rev p{{margin:4px 0 0;font:400 13px/1.55 -apple-system,system-ui,sans-serif;
color:var(--muted)}}
.rate-form{{margin-top:12px}}
.rating-pick{{display:flex;flex-direction:row-reverse;gap:6px;
justify-content:center;margin:4px 0 8px}}
.rating-pick input{{position:absolute;opacity:0;pointer-events:none}}
.rating-pick label{{cursor:pointer;font-size:26px;line-height:40px;
min-width:40px;text-align:center;color:var(--border);
transition:color .15s var(--ease)}}
.rating-pick input:checked ~ label,.rating-pick label:hover,
.rating-pick label:hover ~ label{{color:var(--accent)}}
.rating-pick input:focus-visible + label{{outline:2px solid var(--accent);
outline-offset:2px;border-radius:6px}}
.rated-ok{{border-color:var(--accent);color:var(--ink)}}
.mbio{{margin:12px 0 0;font:400 13.5px/1.55 -apple-system,system-ui,sans-serif;
color:var(--muted)}}
.consent{{display:flex;gap:10px;align-items:flex-start;margin:2px 0 10px;
cursor:pointer;min-height:44px;padding:6px 0}}
.consent input{{width:20px;height:20px;flex:0 0 auto;margin-top:1px;
accent-color:var(--accent);cursor:pointer}}
.consent span{{font:400 12.5px/1.45 -apple-system,system-ui,sans-serif;
color:var(--muted);text-align:left}}
.consent a{{color:var(--accent);font-weight:600}}
.danger{{color:#b23a2e}}
button.danger-btn{{background:#b23a2e;margin-top:8px}}
button.danger-btn:hover{{filter:brightness(1.06)}}
.note{{text-align:center;color:var(--muted);
font:400 14.5px/1.6 -apple-system,system-ui,sans-serif}}
.foot{{text-align:center;color:var(--muted);margin-top:32px;
font:400 12.5px/1.6 -apple-system,system-ui,sans-serif}}
.foot a{{color:var(--muted);display:inline-block;padding:12px 4px}}
</style></head><body>
{cover}<span class="eyebrow">{eyebrow}</span>
<h1>{title}</h1>{body}
<p class="foot">Запись онлайн — без регистрации ·
<a href="/promo">платформа «Боты для записей»</a> ·
<a href="/privacy">обработка данных</a></p>
</body></html>"""


@public_router.get("/c/{slug}")
async def club_by_slug(slug: str, session: AsyncSession = Depends(get_session)):
    """Короткий читаемый адрес клуба: /c/salon-hortensia вместо /club/3.

    Ссылку печатают в QR и диктуют по телефону, поэтому она должна читаться.
    Отвечаем редиректом на /club/<id>, а не дублируем страницу: вся логика
    записи остаётся на одном маршруте, а старые ссылки и QR-коды по
    /club/<id> продолжают работать."""
    from fastapi.responses import RedirectResponse

    tenant = await GlobalRepository(session).get_tenant_by_slug(slug)
    if tenant is None or not tenant.is_active:
        # не подсказываем, существует ли такой адрес
        raise HTTPException(status_code=404, detail="Страница не найдена")
    return RedirectResponse(f"/club/{tenant.id}", status_code=307)


@public_router.get("/club/{tenant_id}", response_class=HTMLResponse)
async def public_club(tenant_id: int, rated: str = "",
                      session: AsyncSession = Depends(get_session)):
    """Публичная страница клуба: список тренировок + запись по имени."""
    import html as _h
    tenant = await _ensure_tenant(session, tenant_id)
    if not tenant.is_active:
        raise HTTPException(status_code=404, detail="Клуб не найден")
    from app.core.config import tenant_suspended
    if tenant_suspended(tenant):
        import html as _h2
        return _PAGE.format(cover="", eyebrow=_eyebrow(tenant), title=_h2.escape(tenant.brand_name or tenant.name),
                            color="#888",
                            body='<div class="card note">Работа клуба временно '
                                 'приостановлена. Обратитесь к тренеру.</div>')
    svc = BookingService(session, tenant_id, tz=tenant.timezone)
    vc = _vcfg(tenant.vertical)

    # ─── витрина клуба: обложка, описание, адрес/телефон, мастера ───
    cover_html = ""
    if tenant.cover_url and tenant.cover_url.startswith("https://"):
        # referrerpolicy: адрес страницы клуба не уходит на чужой хост фото
        cover_html = (f'<div class="cover"><img '
                      f'src="{_h.escape(tenant.cover_url, quote=True)}" '
                      f'alt="" loading="lazy" referrerpolicy="no-referrer"></div>')
    profile_parts = []
    if tenant.about:
        profile_parts.append(
            f'<p class="about">{_h.escape(tenant.about)}</p>')
    info_items = []
    if tenant.address:
        info_items.append(
            f'<div class="m">{_I_PIN} {_h.escape(tenant.address)}</div>')
    if tenant.contact_phone:
        tel = "".join(c for c in tenant.contact_phone
                      if c.isdigit() or c == "+")
        info_items.append(
            f'<div class="m">{_I_PHONE} <a href="tel:{tel}">'
            f'{_h.escape(tenant.contact_phone)}</a></div>')
    if info_items:
        profile_parts.append(
            '<div class="biz-info">' + "".join(info_items) + '</div>')
    strip_masters = await svc.repo.list_masters()   # только активные
    ms_strip_html = ""
    if strip_masters:
        ms = []
        for m in strip_masters[:12]:
            if m.photo_url:
                av = (f'<img src="{_h.escape(m.photo_url, quote=True)}" '
                      f'alt="" loading="lazy" referrerpolicy="no-referrer">')
            else:
                av = f'<span class="mi">{_h.escape(m.name[:1].upper())}</span>'
            spec = (f'<span>{_h.escape(m.specialty)}</span>'
                    if m.specialty else "")
            ms.append(f'<div class="ms-item">{av}'
                      f'<b>{_h.escape(m.name)}</b>{spec}</div>')
        ms_strip_html = '<div class="ms-strip">' + "".join(ms) + '</div>'
    profile_html = "".join(profile_parts)

    rating_stats = (await svc.repo.master_rating_stats()
                    if strip_masters else {})

    def _rate_badge(mid: int, short: bool = False) -> str:
        st = rating_stats.get(mid)
        if not st:
            return ""
        if short:
            return f'<span class="mrate">★ {st[0]:.1f} ({st[1]})</span>'
        return (f'<span class="mrate">★ {st[0]:.1f} · {st[1]} '
                f'{_plural_ratings(st[1])}</span>')

    trainings = await svc.repo.list_upcoming()
    masters = await svc.repo.masters_map() if trainings else {}
    cards = []
    day_order: list[str] = []       # ISO-даты в порядке следования
    day_labels: dict[str, tuple] = {}
    slot_meta: list[tuple] = []     # (id, day_key, local, master_id, is_full)
    for t in trainings:
        start = (t.start_at if t.start_at.tzinfo
                 else t.start_at.replace(tzinfo=_dt.timezone.utc))
        local = start.astimezone(svc.tz)
        day_key = local.date().isoformat()
        if day_key not in day_labels:
            day_order.append(day_key)
            day_labels[day_key] = (_RU_DOW[local.weekday()], local.day,
                                   _RU_MON[local.month - 1])
        active = await svc.repo.get_signups(t.id, "active")
        filled, mx = len(active), t.max_participants
        price = (f'<span class="price">{t.price_minor // 100} ₽</span>'
                 if getattr(t, "price_minor", 0) else "")
        if mx == 1:
            # индивидуальный слот (салон/персональная тренировка):
            # прогресс-бар из одного деления не имеет смысла
            cap = ('<div class="cap"><span class="full">'
                   + vc["web_full"] + '</span></div>' if filled >= mx else
                   '<div class="cap"><span class="free-one">время свободно'
                   '</span></div>')
        else:
            pct = min(100, round(filled / mx * 100)) if mx else 100
            state = (f'<span class="full">{vc["web_full"]}</span>'
                     if filled >= mx
                     else f"<span>свободно: {mx - filled} из {mx}</span>")
            cap = (f'<div class="cap"><div class="bar">'
                   f'<i style="width:{pct}%"></i></div>{state}</div>')
        names = ", ".join(_h.escape(s.name) for s in active[:12])
        who = (f'<div class="who">Записаны: {names}'
               + ("…" if len(active) > 12 else "") + "</div>") if active else ""
        loc = (f'<div class="m">{_I_PIN} {_h.escape(t.location)}</div>'
               if t.location else "")
        m = masters.get(t.master_id) if t.master_id else None
        master_html = ""
        if m:
            if m.photo_url:
                avatar = (f'<img src="{_h.escape(m.photo_url, quote=True)}" '
                          f'alt="" loading="lazy" referrerpolicy="no-referrer">')
            else:
                avatar = f'<span class="mi">{_h.escape(m.name[:1].upper())}</span>'
            spec = (f'<span>{_h.escape(m.specialty)}</span>'
                    if m.specialty else "")
            master_html = (f'<div class="master">{avatar}'
                           f'<div><b>{_h.escape(m.name)}</b>{spec}'
                           f'{_rate_badge(m.id, short=True)}</div></div>')
        slot_meta.append((t.id, day_key, local, t.master_id or 0, filled >= mx))
        cards.append(
            f'<div class="card" data-day="{day_key}" '
            f'data-master="{t.master_id or 0}" id="slot-{t.id}">'
            f'<div class="head"><div class="t">{_h.escape(t.title)}</div>{price}</div>'
            f'<div class="m">{_I_CAL} {svc.format_local(t.start_at)}</div>{loc}'
            f'{master_html}{cap}{who}'
            f'<form method="post" action="/club/{tenant_id}/signup">'
            f'<input type="hidden" name="training_id" value="{t.id}">'
            f'<input name="name" autocomplete="name" required minlength="2" '
            f'maxlength="100" placeholder="Ваше имя" aria-label="Ваше имя">'
            f'<input name="phone" type="tel" autocomplete="tel" required '
            f'minlength="10" maxlength="16" '
            f'placeholder="Телефон (для мастера)" aria-label="Телефон">'
            f'{_CONSENT_SIGNUP}'
            f'<button>Записаться</button></form></div>')
    # Формы «покажите мои записи по телефону» здесь больше нет: номер знают
    # и другие люди, поэтому он не подтверждает личность. Доступ к своим
    # записям — только по личной ссылке, выданной при записи.
    my_form = (f'<div class="card"><div class="t">Мои записи</div>'
               f'<p class="note">Открываются по личной ссылке, которую вы '
               f'получили вместе с подтверждением записи. Не сохранилась — '
               f'обратитесь к администратору клуба.</p>'
               f'<div class="links"><a href="/club/{tenant_id}/my-help">'
               f'Подробнее</a></div></div>')
    days_html = ""
    if len(day_order) > 1:
        chips = "".join(
            f'<button type="button" class="day" data-day="{d}">'
            f'<span class="dow">{day_labels[d][0]}</span>'
            f'<span class="num">{day_labels[d][1]}</span>'
            f'<span class="mon">{day_labels[d][2]}</span></button>'
            for d in day_order)
        days_html = f'<div class="days" role="tablist">{chips}</div>'
    slots_inner = ((days_html + '<div class="list" id="list">'
                    + "".join(cards) + '</div>') if cards else
                   f'<div class="card note">{vc["web_empty"]}</div>')

    if strip_masters:
        # ─── воронка в стиле YClients: меню → мастера/дата → запись ───
        now_local = _dt.datetime.now(svc.tz)

        def _chip_label(local) -> str:
            dd = (local.date() - now_local.date()).days
            hm = local.strftime("%H:%M")
            if dd == 0:
                return f"сегодня {hm}"
            if dd == 1:
                return f"завтра {hm}"
            return f"{_RU_DOW[local.weekday()]} {local.day:02d}.{local.month:02d} {hm}"

        free_by_master: dict[int, list[tuple]] = {}
        for sid, dkey, local, mid, is_full_ in slot_meta:
            if mid and not is_full_:
                free_by_master.setdefault(mid, []).append((sid, dkey, local))

        home_html = (
            '<div id="scr-home" class="scr">' + ms_strip_html +
            '<div class="menu">'
            f'<button type="button" class="menu-row" data-nav="masters">'
            f'<span class="ic">{_I_USERS}</span>Выбрать мастера</button>'
            f'<button type="button" class="menu-row" data-nav="slots">'
            f'<span class="ic">{_I_CAL}</span>Выбрать дату и время</button>'
            '</div></div>')

        mcards = []
        for m in strip_masters:
            if m.photo_url:
                av = (f'<img src="{_h.escape(m.photo_url, quote=True)}" '
                      f'alt="" loading="lazy" referrerpolicy="no-referrer">')
            else:
                av = f'<span class="mi">{_h.escape(m.name[:1].upper())}</span>'
            spec = (f'<span>{_h.escape(m.specialty)}</span>'
                    if m.specialty else "")
            mname_attr = _h.escape(m.name, quote=True)
            frees = free_by_master.get(m.id, [])[:5]
            if frees:
                chips = "".join(
                    f'<button type="button" class="tchip" data-slot="{sid}" '
                    f'data-m="{m.id}" data-mname="{mname_attr}" '
                    f'data-day="{dkey}">{_chip_label(local)}</button>'
                    for sid, dkey, local in frees)
                chips += (f'<button type="button" class="tchip more" '
                          f'data-all-of="{m.id}" data-mname="{mname_attr}" '
                          f'data-first-day="{frees[0][1]}">Все времена</button>')
                chips_html = f'<div class="chips">{chips}</div>'
            else:
                chips_html = '<div class="mnone">Свободных окон пока нет</div>'
            # отзывы с текстом (последние 3) + форма оценки
            revs = await svc.repo.list_master_reviews(m.id, limit=3)
            rev_items = "".join(
                f'<div class="rev"><b>{_h.escape(r.author_name)}</b> '
                f'<span class="rstars">{"★" * r.rating}{"☆" * (5 - r.rating)}'
                f'</span><p>{_h.escape(r.text)}</p></div>'
                for r in revs)
            rev_html = (f'<details class="mrev"><summary>Отзывы</summary>'
                        f'{rev_items}</details>') if rev_items else ""
            stars_input = "".join(
                f'<input type="radio" name="rating" value="{v}" '
                f'id="mr{m.id}v{v}"{" checked" if v == 5 else ""}>'
                f'<label for="mr{m.id}v{v}">★</label>'
                for v in (5, 4, 3, 2, 1))
            rate_form = (
                f'<details class="mrev"><summary>Оценить мастера</summary>'
                f'<form method="post" action="/club/{tenant_id}/rate" '
                f'class="rate-form">'
                f'<input type="hidden" name="master_id" value="{m.id}">'
                f'<div class="rating-pick">{stars_input}</div>'
                f'<input name="name" autocomplete="name" required minlength="2" '
                f'maxlength="100" placeholder="Ваше имя" aria-label="Ваше имя">'
                f'<input name="text" maxlength="300" '
                f'placeholder="Короткий отзыв (необязательно)">'
                f'{_CONSENT_RATE}'
                f'<button>Отправить оценку</button>'
                f'<p class="note" style="margin-top:8px">Оценка принимается '
                f'после визита и только по вашей личной ссылке — той, что '
                f'пришла вместе с подтверждением записи.</p>'
                f'</form></details>')
            bio_html = (f'<p class="mbio">{_h.escape(m.bio)}</p>'
                        if getattr(m, "bio", "") else "")
            mcards.append(
                f'<div class="card mcard"><div class="master">{av}'
                f'<div><b>{_h.escape(m.name)}</b>{spec}'
                f'{_rate_badge(m.id)}</div></div>{bio_html}'
                f'{chips_html}{rev_html}{rate_form}</div>')
        masters_scr = (
            '<div id="scr-masters" class="scr">'
            '<div class="backrow"><button type="button" class="backbtn" '
            'data-back aria-label="Назад">←</button>'
            '<span class="scr-title">Выбрать мастера</span></div>'
            + "".join(mcards) + '</div>')

        slots_scr = (
            '<div id="scr-slots" class="scr">'
            '<div class="backrow"><button type="button" class="backbtn" '
            'data-back aria-label="Назад">←</button>'
            '<span class="scr-title">Выбрать дату и время</span>'
            '<button type="button" id="mfilter"></button></div>'
            + slots_inner + my_form + '</div>')

        if rated == "1":
            rated_note = ('<div class="card note rated-ok">Спасибо! '
                          'Оценка сохранена.</div>')
        elif rated == "nosession":
            rated_note = (
                '<div class="card note">Оценку можно оставить только по '
                'личной ссылке, которую вы получили вместе с подтверждением '
                'записи: так мы знаем, что оценка от вас, а не от того, кто '
                'узнал ваш номер.</div>')
        elif rated == "novisit":
            # честно объясняем, почему оценка не принята
            rated_note = (
                '<div class="card note">Оценку можно оставить после визита: '
                f'у вас нет отмеченного посещения у выбранного '
                f'{_h.escape(vc["master_word"])}а. Отметку ставит '
                f'{_h.escape(vc["master_word"])} или администратор после '
                'занятия — если визит был, попросите её проставить.</div>')
        else:
            rated_note = ""
        body = (profile_html + rated_note + home_html + masters_scr
                + slots_scr + _CLUB_JS)
    else:
        # клубы без мастеров — прежний простой вид (список слотов сразу)
        body = (profile_html + ms_strip_html
                + (slots_inner + my_form + _CLUB_JS if cards
                   else slots_inner + my_form))
    title = tenant.brand_name or tenant.name
    return _PAGE.format(cover=cover_html, eyebrow=_eyebrow(tenant),
                        title=_h.escape(title),
                        color=_safe_color(tenant.brand_color), body=body)


@public_router.post("/club/{tenant_id}/signup", response_class=HTMLResponse)
async def public_signup(tenant_id: int,
                        request: Request,
                        training_id: int = Form(...),
                        name: str = Form(...),
                        phone: str = Form(...),
                        consent: str = Form(""),
                        session: AsyncSession = Depends(get_session)):
    """Запись с публичной страницы: имя + телефон (телефон видит тренер)."""
    import html as _h
    if not consent.strip():
        from app.api.public_style import CONSENT_ERROR
        raise HTTPException(status_code=400, detail=CONSENT_ERROR)
    await _rate_guard(session, client_ip(request), scope="signup",
                      tenant_id=tenant_id)
    tenant = await _ensure_tenant(session, tenant_id)
    if not tenant.is_active:
        raise HTTPException(status_code=404, detail="Клуб не найден")
    from app.core.config import tenant_suspended
    if tenant_suspended(tenant):
        import html as _h2
        return _PAGE.format(cover="", eyebrow=_eyebrow(tenant), title=_h2.escape(tenant.brand_name or tenant.name),
                            color="#888",
                            body='<div class="card note">Работа клуба временно '
                                 'приостановлена. Обратитесь к тренеру.</div>')
    name = name.strip()[:100]
    digits = "".join(c for c in phone if c.isdigit())
    if len(name) < 2 or not (10 <= len(digits) <= 15):
        raise HTTPException(status_code=400, detail="Некорректные имя или телефон")
    svc = BookingService(session, tenant_id, tz=tenant.timezone)
    # телефон больше не идентификатор: он хранится зашифрованным в
    # web_customers, а в записи попадает только суррогатный id
    from app.core.phones import KeyUnavailable
    try:
        uid = await svc.repo.web_customer_id(digits, name)
    except KeyUnavailable:
        # ключ шифрования телефонов недоступен — записать нельзя, но это
        # проблема конфигурации сервера, а не посетителя: не пугаем его
        # деталями и не роняем 500
        logger.error("Запись невозможна: недоступен ключ телефонов")
        raise HTTPException(
            status_code=503,
            detail="Онлайн-запись временно недоступна, запишитесь по "
                   "телефону клуба") from None
    await svc.repo.upsert_subscriber("web", uid, name)
    # Согласие пишем ДО sign_up: он коммитит транзакцию сам, и запись
    # согласия после него ушла бы отдельной. Сейчас обе строки попадают в
    # один коммит — не сохранилось согласие, не появится и запись.
    await svc.repo.record_consent(
        platform="web", user_id=uid, purpose="booking",
        consent_text=_consent_text("имени и телефона для записи"))
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
        manage = await _issue_manage_link(svc, tenant_id, uid)
        await session.commit()
        cancel_link = (
            f'<a href="/club/{tenant_id}/cancel'
            f'?t={training_id}&u={uid}&s={token}">Отменить эту запись</a><br>'
            # личная ссылка: по ней видны все свои записи, выгрузка и
            # удаление данных — без ввода телефона
            f'<a href="{manage}">Мои записи и данные</a><br>')
    body = (f'<div class="card"><div class="ok-icon">{icon}</div>'
            f'<p class="ok">{msg}</p>'
            f'<div class="links">{cancel_link}<a href="/club/{tenant_id}">'
            f'← к списку тренировок</a></div></div>')
    title = tenant.brand_name or tenant.name
    return _PAGE.format(cover="", eyebrow=_eyebrow(tenant), title=_h.escape(title),
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


@public_router.post("/club/{tenant_id}/rate", response_class=HTMLResponse)
async def public_rate_master(tenant_id: int,
                             request: Request,
                             master_id: int = Form(...),
                             rating: int = Form(...),
                             name: str = Form(...),
                             text: str = Form(""),
                             consent: str = Form(""),
                             session: AsyncSession = Depends(get_session)):
    """Оценка мастера.

    Личность подтверждает сессия управления (cookie, полученная по личной
    ссылке), а не номер телефона: номер знают и другие люди, и раньше
    достаточно было ввести чужой, чтобы поставить оценку за него.

    Сам факт визита тоже проверяется: нужна активная запись к ЭТОМУ
    мастеру на прошедшее занятие с отметкой явки. Записаться и не прийти
    — не повод оценивать.

    Одна актуальная оценка клиента о мастере: повторная заменяет прежнюю.
    Это же правило закреплено в БД уникальным ключом
    (tenant_id, master_id, user_id) — обработчик здесь не единственная
    защита."""
    from fastapi.responses import RedirectResponse
    if not consent.strip():
        from app.api.public_style import CONSENT_ERROR
        raise HTTPException(status_code=400, detail=CONSENT_ERROR)
    await _rate_guard(session, client_ip(request), scope="rate",
                      tenant_id=tenant_id)
    tenant = await _ensure_tenant(session, tenant_id)
    if not tenant.is_active:
        raise HTTPException(status_code=404, detail="Клуб не найден")
    name = name.strip()[:100]
    if len(name) < 2 or not (1 <= rating <= 5):
        raise HTTPException(status_code=400, detail="Некорректные данные")

    cookie = request.cookies.get(_manage_cookie(tenant_id), "")
    svc = BookingService(session, tenant_id, tz=tenant.timezone)
    sess = (await svc.repo.resolve_manage_session(_manage_hash(cookie))
            if cookie else None)
    if sess is None:
        # без подтверждённой сессии оценку не принимаем и не намекаем,
        # существует ли такой клиент
        return RedirectResponse(f"/club/{tenant_id}?rated=nosession",
                                status_code=303)
    _platform, uid = sess

    if await svc.repo.get_master(master_id) is None:
        raise HTTPException(status_code=404, detail="Мастер не найден")
    if not await svc.repo.has_visited_master(master_id, "web", uid):
        return RedirectResponse(f"/club/{tenant_id}?rated=novisit",
                                status_code=303)
    await svc.repo.upsert_master_review(
        master_id=master_id, user_id=uid,
        author_name=name, rating=rating, text=text.strip()[:500])
    await svc.repo.record_consent(
        platform="web", user_id=uid, purpose="master_review",
        consent_text=_consent_text(
            "имени и текста отзыва для оценки мастера"))
    await session.commit()
    return RedirectResponse(f"/club/{tenant_id}?rated=1", status_code=303)


def _cancel_token(tenant_id: int, training_id: int, uid: int) -> str:
    """HMAC-подпись ссылки отмены — отменить может только тот, кто получил
    ссылку при записи (или знает телефон). Ключ — jwt_secret (в проде
    обязательно случайный, см. assert_production_secrets)."""
    import hashlib
    import hmac as _hmac
    msg = f"{tenant_id}:{training_id}:{uid}".encode()
    return _hmac.new(settings.jwt_secret.encode(), msg,
                     hashlib.sha256).hexdigest()[:32]


def _new_manage_token() -> tuple[str, str]:
    """(сам токен для ссылки, его SHA-256 для базы). Токен случайный: из
    базы его восстановить нельзя, а при удалении данных — отзывается."""
    import hashlib
    import secrets
    token = secrets.token_urlsafe(24)
    return token, hashlib.sha256(token.encode()).hexdigest()


# Сколько живёт сессия управления своими записями. Короткая: это доступ к
# персональным данным, а не «запомнить меня».
MANAGE_SESSION_MAX_AGE = 60 * 60 * 2      # 2 часа


def _manage_cookie(tenant_id: int) -> str:
    """Своя cookie на каждый клуб: сессия одного клуба не должна открывать
    данные в другом, даже если человек ходит в оба."""
    return f"manage_{tenant_id}"


def _no_store(resp):
    """Заголовки для страниц с персональными данными.

    no-store — чтобы список записей и телефон не оставались в кеше браузера
    и промежуточных прокси; no-referrer — чтобы адрес страницы (а с ним и
    факт визита) не уезжал на сторонние сайты по ссылкам."""
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _html_no_store(html: str):
    from fastapi.responses import HTMLResponse as _HR
    return _no_store(_HR(html))


def _manage_hash(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


async def _issue_manage_link(svc, tenant_id: int, uid: int) -> str:
    token, token_hash = _new_manage_token()
    await svc.repo.issue_manage_token("web", uid, token_hash)
    return f"/club/{tenant_id}/m/{token}"


@public_router.get("/club/{tenant_id}/cancel", response_class=HTMLResponse)
async def public_cancel_confirm(tenant_id: int, t: int, u: int, s: str,
                                session: AsyncSession = Depends(get_session)):
    """Страница подтверждения отмены. Сама отмена — POST ниже.

    Раньше отмена происходила прямо по переходу по ссылке. Ссылку видит не
    только человек: мессенджеры открывают её ради превью, браузеры делают
    предзагрузку, антивирусы и корпоративные фильтры проверяют содержимое —
    и запись отменялась сама собой, без участия владельца."""
    import hmac as _hmac
    import html as _h
    tenant = await _ensure_tenant(session, tenant_id)
    if not _hmac.compare_digest(s, _cancel_token(tenant_id, t, u)):
        raise HTTPException(status_code=403, detail="Неверная ссылка отмены")
    svc = BookingService(session, tenant_id, tz=tenant.timezone)
    training = await svc.repo.get_training(t)
    what = (f'«{_h.escape(training.title)}» '
            f'{_h.escape(svc.format_local(training.start_at))}'
            if training else "эту запись")
    body = (f'<div class="card"><div class="t">Отменить запись?</div>'
            f'<p class="note">{what}</p>'
            f'<form method="post" action="/club/{tenant_id}/cancel">'
            f'<input type="hidden" name="t" value="{t}">'
            f'<input type="hidden" name="u" value="{u}">'
            f'<input type="hidden" name="s" value="{_h.escape(s)}">'
            f'<button class="danger-btn">Да, отменить</button></form>'
            f'<div class="links"><a href="/club/{tenant_id}">'
            f'← вернуться к списку</a></div></div>')
    title = tenant.brand_name or tenant.name
    return _PAGE.format(cover="", eyebrow=_eyebrow(tenant),
                        title=_h.escape(title),
                        color=_safe_color(tenant.brand_color), body=body)


@public_router.post("/club/{tenant_id}/cancel", response_class=HTMLResponse)
async def public_cancel(tenant_id: int,
                        t: int = Form(...),
                        u: int = Form(...),
                        s: str = Form(...),
                        session: AsyncSession = Depends(get_session)):
    """Отмена записи — только осознанным действием (POST из формы)."""
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
    return _PAGE.format(cover="", eyebrow=_eyebrow(tenant), title=_h.escape(title),
                        color=_safe_color(tenant.brand_color), body=body)


@public_router.get("/club/{tenant_id}/qr")
async def club_qr(tenant_id: int, request: Request,
                  session: AsyncSession = Depends(get_session)):
    """PNG QR-код со ссылкой на страницу записи клуба — для печати в зале."""
    import io
    import qrcode
    from fastapi.responses import StreamingResponse
    from app.core.club_url import club_site_url

    tenant = await _ensure_tenant(session, tenant_id)
    # QR печатают и вешают в зале: если у клуба свой сайт, код обязан вести
    # туда, иначе распечатанный QR ведёт не туда, куда клиент рассчитывает.
    try:
        url = club_site_url(tenant)
    except RuntimeError:
        base = (settings.public_base_url or str(request.base_url).rstrip("/"))
        url = f"{base}/club/{tenant_id}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


def _manage_page(tenant, tenant_id: int, rows, svc,
                 notice: str = "") -> str:
    import html as _h
    parts = []
    if notice:
        parts.append(f'<div class="card note">{_h.escape(notice)}</div>')
    if not rows:
        parts.append('<div class="card note">Активных записей нет.</div>')
    for t, status, position in rows:
        chip = ('<span class="chip">записаны</span>' if status == "active"
                else f'<span class="chip q">в очереди №{position}</span>')
        parts.append(
            f'<div class="card">'
            f'<div class="head"><div class="t">{_h.escape(t.title)}</div>'
            f'{chip}</div>'
            f'<div class="m">{_I_CAL} {svc.format_local(t.start_at)}</div>'
            f'<form method="post" action="/club/{tenant_id}/manage/cancel">'
            f'<input type="hidden" name="training_id" value="{t.id}">'
            f'<button class="danger-btn">Отменить запись</button></form>'
            f'</div>')
    parts.append(
        f'<div class="card"><div class="t">Мои данные</div>'
        f'<p class="note">Можно забрать копию всего, что о вас хранится, '
        f'или удалить это.</p>'
        f'<div class="links" style="margin-bottom:12px">'
        f'<a href="/club/{tenant_id}/manage/export">Скачать мои данные</a>'
        f'</div>'
        f'<form method="post" action="/club/{tenant_id}/manage/forget">'
        f'<button class="danger-btn">Удалить мои данные</button></form>'
        f'<p class="note" style="margin-top:8px">Удаление отменит записи и '
        f'уберёт имя, телефон и оценки. Эта ссылка перестанет работать.</p>'
        f'</div>')
    parts.append(f'<div class="links"><a href="/club/{tenant_id}">'
                 f'← к списку</a></div>')
    title = tenant.brand_name or tenant.name
    return _PAGE.format(cover="", eyebrow=_eyebrow(tenant),
                        title=_h.escape(title),
                        color=_safe_color(tenant.brand_color),
                        body="".join(parts))


@public_router.get("/club/{tenant_id}/m/{token}", response_class=HTMLResponse)
async def public_manage_enter(tenant_id: int, token: str,
                              session: AsyncSession = Depends(get_session)):
    """Вход по персональной ссылке: обмениваем ОДНОРАЗОВУЮ ссылку на короткую
    сессию и уводим на URL без токена.

    Токен ссылки в адресе живёт дольше визита: он остаётся в истории
    браузера, в Referer при переходе на внешний сайт и в access-логах любого
    прокси. Поэтому: (1) сама ссылка одноразовая — второй переход по ней уже
    не сработает (пересланная/утёкшая ссылка бесполезна); (2) в cookie
    кладётся НЕ она, а отдельный секрет короткой сессии; дальше человек
    работает по чистому адресу."""
    from fastapi.responses import RedirectResponse

    tenant = await _ensure_tenant(session, tenant_id)
    svc = BookingService(session, tenant_id, tz=tenant.timezone)

    session_token, session_hash = _new_manage_token()   # секрет сессии
    uid = await svc.repo.consume_manage_token(
        _manage_hash(token), session_hash,
        session_ttl_min=MANAGE_SESSION_MAX_AGE // 60)
    await session.commit()
    if uid is None:
        # ссылка недействительна, истекла или УЖЕ использована
        raise HTTPException(status_code=404,
                            detail="Ссылка недействительна или уже открыта. "
                                   "Попросите у клуба новую.")

    resp = RedirectResponse(f"/club/{tenant_id}/manage", status_code=303)
    resp.set_cookie(
        _manage_cookie(tenant_id), session_token,
        httponly=True,                       # недоступна из JavaScript
        secure=not settings.admin_dev_login,  # по HTTP только в отладке
        samesite="lax",
        max_age=MANAGE_SESSION_MAX_AGE,
        path=f"/club/{tenant_id}",           # не уходит на другие клубы
    )
    _no_store(resp)
    return resp


async def _manage_session(request: Request, session, tenant_id: int):
    """(tenant, svc, uid) по короткой cookie-сессии или 404."""
    tenant = await _ensure_tenant(session, tenant_id)
    svc = BookingService(session, tenant_id, tz=tenant.timezone)
    cookie = request.cookies.get(_manage_cookie(tenant_id), "")
    sess = (await svc.repo.resolve_manage_session(_manage_hash(cookie))
            if cookie else None)
    if sess is None:
        raise HTTPException(status_code=404,
                            detail="Сессия истекла. Прежняя ссылка "
                                   "одноразовая и повторно не сработает — "
                                   "попросите у клуба новую персональную "
                                   "ссылку.")
    _platform, uid = sess
    return tenant, svc, uid


@public_router.get("/club/{tenant_id}/manage", response_class=HTMLResponse)
async def public_manage(tenant_id: int, request: Request,
                        session: AsyncSession = Depends(get_session)):
    """Свои записи и данные. Доступ — по cookie, выданной персональной
    ссылкой; телефон, который знают и другие люди, здесь ничего не даёт."""
    tenant, svc, uid = await _manage_session(request, session, tenant_id)
    rows = await svc.my_trainings("web", uid)
    return _html_no_store(_manage_page(tenant, tenant_id, rows, svc))


@public_router.post("/club/{tenant_id}/manage/cancel",
                    response_class=HTMLResponse)
async def public_manage_cancel(tenant_id: int, request: Request,
                               training_id: int = Form(...),
                               session: AsyncSession = Depends(get_session)):
    tenant, svc, uid = await _manage_session(request, session, tenant_id)
    res = await svc.cancel_signup(training_id, "web", uid)
    await session.commit()
    if res.get("cancelled"):
        await _notify_group_card_changed(tenant_id, training_id)
    rows = await svc.my_trainings("web", uid)
    notice = "Запись отменена." if res.get("cancelled") else "Запись не найдена."
    return _html_no_store(_manage_page(tenant, tenant_id, rows, svc, notice))


@public_router.get("/club/{tenant_id}/manage/export")
async def public_manage_export(tenant_id: int, request: Request,
                               session: AsyncSession = Depends(get_session)):
    """Выгрузка своих данных — то, что о человеке хранится в этом клубе."""
    from fastapi.responses import JSONResponse

    tenant, svc, uid = await _manage_session(request, session, tenant_id)
    rows = await svc.my_trainings("web", uid)
    subs = await svc.repo.aliases_map_all()
    data = {
        "клуб": tenant.brand_name or tenant.name,
        "выгружено": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        # свой номер человек вправе увидеть: расшифровываем для него самого
        "телефон": await svc.repo.web_phone(uid),
        "подпись_у_администратора": subs.get(("web", uid), ""),
        "записи": [
            {"занятие": t.title,
             "начало": svc.format_local(t.start_at),
             "статус": status,
             "место_в_очереди": position}
            for t, status, position in rows
        ],
    }
    resp = JSONResponse(
        data,
        headers={"Content-Disposition":
                 f'attachment; filename="my-data-club-{tenant_id}.json"'},
        media_type="application/json; charset=utf-8")
    _no_store(resp)
    return resp


@public_router.post("/club/{tenant_id}/manage/forget",
                    response_class=HTMLResponse)
async def public_manage_forget(tenant_id: int, request: Request,
                               session: AsyncSession = Depends(get_session)):
    """Удаление своих данных в этом клубе."""
    import html as _h

    tenant, svc, uid = await _manage_session(request, session, tenant_id)
    rows = await svc.my_trainings("web", uid)
    affected = [t.id for t, _s, _p in rows]
    removed = await svc.repo.forget_user("web", uid)
    await session.commit()
    for tr_id in affected:
        await _notify_group_card_changed(tenant_id, tr_id)
    body = (f'<div class="card"><div class="ok-icon">{_I_CHECK}</div>'
            f'<p class="ok">Данные удалены.</p>'
            f'<p class="note">Записей: {removed.get("signups", 0)}, '
            f'оценок: {removed.get("master_reviews", 0)}, '
            f'профиль с телефоном: {removed.get("subscribers", 0)}. '
            f'Ссылка управления больше не работает.</p>'
            f'<div class="links"><a href="/club/{tenant_id}">'
            f'← к списку занятий</a></div></div>')
    title = tenant.brand_name or tenant.name
    resp = _html_no_store(_PAGE.format(
        cover="", eyebrow=_eyebrow(tenant), title=_h.escape(title),
        color=_safe_color(tenant.brand_color), body=body))
    # сессию гасим: данных, к которым она вела, больше нет
    resp.delete_cookie(_manage_cookie(tenant_id), path=f"/club/{tenant_id}")
    return resp


def _my_help_body(tenant_id: int) -> str:
    """Единственный ответ на вопрос «где мои записи» — одинаковый для всех.

    Текст НЕ зависит от введённого номера: ни от его корректности, ни от
    того, есть ли такой клиент. Иначе по различию ответов можно было бы
    перебором узнать, кто ходит в этот клуб."""
    return (f'<div class="card"><div class="t">Доступ к своим записям</div>'
            f'<p class="note">По номеру телефона записи не показываются: '
            f'номер знают и другие люди, поэтому он не подтверждает, что вы '
            f'— это вы.</p>'
            f'<p class="note">Когда вы записывались через сайт, вместе с '
            f'подтверждением выдавалась личная ссылка «Мои записи и данные». '
            f'Откройте её — там список записей, отмена, выгрузка и удаление '
            f'данных.</p>'
            f'<p class="note">Ссылка не сохранилась — обратитесь к '
            f'администратору клуба: он найдёт вашу запись, отменит или '
            f'перенесёт её.</p>'
            f'<div class="links"><a href="/club/{tenant_id}">'
            f'← к списку занятий</a></div></div>')


@public_router.post("/club/{tenant_id}/my", response_class=HTMLResponse)
async def public_my(tenant_id: int,
                    request: Request,
                    phone: str = Form(""),
                    session: AsyncSession = Depends(get_session)):
    """
    Восстановление доступа по одному номеру телефона БОЛЬШЕ НЕ РАБОТАЕТ.

    Раньше этот обработчик по введённому номеру показывал чужие записи,
    ссылки их отмены и выдавал новую персональную ссылку управления —
    то есть номер работал как пароль. Номер не секрет: его знают
    администратор, коллеги, любой, кто видел запись, и он же угадывается
    перебором.

    Подтверждённого независимого канала (SMS или привязанный аккаунт
    Telegram/ВК) у веб-записи сейчас нет, поэтому безопасное поведение по
    умолчанию — не восстанавливать доступ вовсе. Ответ одинаков для
    существующего и несуществующего номера: по нему нельзя проверить,
    записан ли человек в этот клуб.

    Маршрут сохранён (а не удалён), чтобы старые вкладки и закладки не
    получали 404 и человек видел объяснение, что делать дальше.
    """
    import html as _h
    await _rate_guard(session, client_ip(request), scope="my",
                      tenant_id=tenant_id)
    tenant = await _ensure_tenant(session, tenant_id)
    title = tenant.brand_name or tenant.name
    return _PAGE.format(cover="", eyebrow=_eyebrow(tenant), title=_h.escape(title),
                        color=_safe_color(tenant.brand_color),
                        body=_my_help_body(tenant_id))


@public_router.get("/club/{tenant_id}/my-help", response_class=HTMLResponse)
async def public_my_help(tenant_id: int,
                         session: AsyncSession = Depends(get_session)):
    """Как попасть в свои записи. Ничего не ищет и ничего не выдаёт."""
    import html as _h
    tenant = await _ensure_tenant(session, tenant_id)
    title = tenant.brand_name or tenant.name
    return _PAGE.format(cover="", eyebrow=_eyebrow(tenant), title=_h.escape(title),
                        color=_safe_color(tenant.brand_color),
                        body=_my_help_body(tenant_id))


@public_router.get("/promo", response_class=HTMLResponse)
async def promo_page(session: AsyncSession = Depends(get_session)):
    """Промо-страница продукта (лендинг). Контакты и цены — в promo_page.py.
    Ссылку на демонстрационную страницу записи берём из БД: показывать
    вместо неё клуб реального заказчика нельзя."""
    from app.api.promo_page import render_promo_page
    g = GlobalRepository(session)
    demos = await g.list_demo_tenants()
    # витрина направлений; при единственном демо-клубе — прежняя карточка
    return render_promo_page(await g.demo_tenant_id(),
                             demos if len(demos) > 1 else None)


@public_router.get("/faq", response_class=HTMLResponse)
async def faq_page(v: str = ""):
    """FAQ для клиентов. ?v=sport|beauty отдаёт вопросы одного направления —
    так переключатель на странице работает и без JavaScript."""
    from app.api.faq_page import render_faq_page
    return render_faq_page(v or "all")


@public_router.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    """Политика обработки персональных данных — в privacy_page.py."""
    from app.api.privacy_page import PRIVACY_HTML
    return PRIVACY_HTML


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
                         consent: str = Form(""),
                         session: AsyncSession = Depends(get_session)):
    """Приём нового отзыва: honeypot-поле + лимит по IP против спама,
    отзыв уходит в модерацию (approved=False) и не виден на странице сразу."""
    from fastapi.responses import RedirectResponse
    from app.api.reviews_page import render_reviews_page
    ip = client_ip(request)
    g = GlobalRepository(session)

    if website.strip():
        # honeypot заполнен — почти наверняка бот; тихо "принимаем",
        # чтобы не подсказывать боту, что его вычислили
        return RedirectResponse(url="/reviews?sent=1", status_code=303)

    from app.api import rate_limit
    ok, retry = await rate_limit.allow(session, scope="site-review",
                                       tenant_id=None, client=ip)
    if not ok:
        reviews = await g.list_approved_reviews()
        return render_reviews_page(
            reviews,
            notice=f"Слишком много попыток, попробуйте через {retry} с.",
            notice_kind="err")

    if not consent.strip():
        from app.api.public_style import CONSENT_ERROR
        reviews = await g.list_approved_reviews()
        return render_reviews_page(reviews, notice=CONSENT_ERROR,
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
    await g.record_platform_consent(
        purpose="platform_review",
        consent_text=_consent_text(
            "имени и текста отзыва для публикации на этой странице"))
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
