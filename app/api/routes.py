"""REST API: управление тенантами и тренировками (защищено токеном админа)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
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
from app.core.config import settings
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
    from sqlalchemy import delete, text
    tenant = await _ensure_tenant(session, tenant_id)
    # удаляем зависимые записи в безопасном порядке
    for table in ("signups", "payments", "outbox", "subscribers",
                  "trainings", "memberships", "groups"):
        try:
            await session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :tid"),
                {"tid": tenant_id})
        except Exception:
            pass  # таблицы может не быть в этой редакции — пропускаем
    await session.execute(
        text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
    await session.commit()
    return {"ok": True, "deleted_tenant_id": tenant_id}


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
        raise HTTPException(status_code=400, detail=str(e))
    return {"confirmation_url": url}
