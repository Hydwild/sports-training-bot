"""
Сервис платежей: создаёт платёж за тренировку и обрабатывает вебхук
провайдера. При успешном платеже автоматически проставляет paid=True
у записи участника — он попадает в список оплативших.

Безопасность:
  - вебхук проверяется провайдером (verify_webhook: IP/подпись),
  - идемпотентность: повторный вебхук по тому же платежу не зачисляет дважды,
  - суммы и привязка берутся из metadata, сверяются с нашим Payment.
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.repo import GlobalRepository, TenantRepository
from app.services import payments as providers

logger = logging.getLogger("payment-service")


class PaymentService:
    def __init__(self, session: AsyncSession, tenant_id: int) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.repo = TenantRepository(session, tenant_id)

    async def start_payment(self, *, training_id: int, platform: str,
                            user_id: int, provider_name: str,
                            return_url: str) -> str:
        """Создаёт платёж у провайдера, сохраняет запись, возвращает URL оплаты."""
        training = await self.repo.get_training(training_id)
        if not training:
            raise ValueError("Тренировка не найдена")
        if training.price_minor <= 0:
            raise ValueError("Тренировка бесплатная — оплата не требуется")

        provider = providers.get_provider(provider_name)
        signup = await self.repo.get_user_signup(training_id, platform, user_id)

        created = await provider.create_payment(
            amount_minor=training.price_minor,
            currency=training.currency,
            description=f"Оплата: {training.title}",
            return_url=return_url,
            metadata={
                "tenant_id": self.tenant_id,
                "training_id": training_id,
                "platform": platform,
                "user_id": user_id,
            },
        )
        await self.repo.add_payment(
            training_id=training_id,
            signup_id=signup.id if signup else None,
            platform=platform, user_id=user_id, provider=provider_name,
            provider_payment_id=created.provider_payment_id,
            amount_minor=training.price_minor, currency=training.currency,
            status="pending",
        )
        await self.session.commit()
        return created.confirmation_url


async def handle_webhook(session: AsyncSession, provider_name: str,
                         *, body: bytes, headers: dict, remote_ip: str | None,
                         payload: dict) -> bool:
    """
    Глобальная обработка вебхука (тенант берётся из metadata/найденного платежа).
    Возвращает True, если платёж успешно зачтён (или уже был зачтён).
    """
    provider = providers.get_provider(provider_name)
    if not provider.verify_webhook(body=body, headers=headers, remote_ip=remote_ip):
        logger.warning("Вебхук %s не прошёл проверку (ip=%s)", provider_name, remote_ip)
        return False

    parsed = provider.parse_webhook(payload)
    pid = parsed.get("provider_payment_id")
    status = parsed.get("status")
    if not pid:
        return False

    g = GlobalRepository(session)
    # блокируем строку платежа: провайдеры повторяют вебхуки при таймауте,
    # без лока два почти одновременных запроса могут оба пройти проверку
    # статуса ниже до commit друг друга и задвоить уведомление об оплате.
    payment = await g.get_payment_by_provider_id_global_for_update(pid)
    if payment is None:
        logger.warning("Вебхук: платёж %s не найден в базе", pid)
        return False

    # Идемпотентность: если уже succeeded — ничего не делаем повторно.
    if payment.status == "succeeded":
        return True

    if status == "succeeded":
        payment.status = "succeeded"
        # проставляем оплату у записи участника
        repo = TenantRepository(session, payment.tenant_id)
        signup = None
        if payment.signup_id:
            signup = await repo.get_signup_by_id(payment.signup_id)
        if signup is None:
            signup = await repo.get_user_signup(
                payment.training_id, payment.platform, payment.user_id)
        if signup:
            signup.paid = True
            await repo.enqueue(payment.platform, payment.user_id,
                               "✅ Оплата получена. Спасибо!")
        await session.commit()
        return True
    elif status in ("canceled", "cancelled"):
        payment.status = "canceled"
        await session.commit()
        return False

    return False
