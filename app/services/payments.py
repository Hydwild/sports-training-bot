"""
Платёжный слой. Абстрактный провайдер + две реализации:
  - YooKassaProvider — рабочая (создание платежа + проверка вебхука),
  - StripeProvider — каркас с теми же методами (точки подключения помечены).

Сервис платежей не зависит от конкретного провайдера — выбирает по настройке
клуба. Безопасность вебхука и идемпотентность реализованы в PaymentService.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core.config import settings

logger = logging.getLogger("payments")


@dataclass
class PaymentCreated:
    provider_payment_id: str
    confirmation_url: str


class PaymentProvider(ABC):
    name: str

    @abstractmethod
    async def create_payment(self, *, amount_minor: int, currency: str,
                             description: str, return_url: str,
                             metadata: dict) -> PaymentCreated:
        ...

    @abstractmethod
    def verify_webhook(self, *, body: bytes, headers: dict,
                       remote_ip: str | None) -> bool:
        ...

    @abstractmethod
    def parse_webhook(self, payload: dict) -> dict:
        """Возвращает {'provider_payment_id', 'status', 'metadata'}."""
        ...


# ---------- ЮKassa (рабочая) ----------

# Доверенные подсети уведомлений ЮKassa (из их документации).
_YOOKASSA_NETS = [
    "185.71.76.0/27", "185.71.77.0/27", "77.75.153.0/25",
    "77.75.156.11/32", "77.75.156.35/32", "77.75.154.128/25",
    "2a02:5180::/32",
]


class YooKassaProvider(PaymentProvider):
    name = "yookassa"

    def __init__(self) -> None:
        self._configured = bool(settings.yookassa_shop_id
                                and settings.yookassa_secret_key)
        if self._configured:
            from yookassa import Configuration
            Configuration.configure(settings.yookassa_shop_id,
                                    settings.yookassa_secret_key)

    async def create_payment(self, *, amount_minor, currency, description,
                             return_url, metadata) -> PaymentCreated:
        if not self._configured:
            raise RuntimeError("ЮKassa не настроена (нет shop_id/secret_key)")
        # SDK синхронный — выполняем в thread executor, чтобы не блокировать loop
        return await asyncio.to_thread(
            self._create_sync, amount_minor, currency, description,
            return_url, metadata)

    def _create_sync(self, amount_minor, currency, description, return_url,
                     metadata) -> PaymentCreated:
        import uuid
        from yookassa import Payment as YkPayment
        value = f"{amount_minor / 100:.2f}"
        payment = YkPayment.create({
            "amount": {"value": value, "currency": currency},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "capture": True,
            "description": description,
            "metadata": metadata,
        }, str(uuid.uuid4()))  # ключ идемпотентности на стороне ЮKassa
        return PaymentCreated(provider_payment_id=payment.id,
                              confirmation_url=payment.confirmation.confirmation_url)

    def verify_webhook(self, *, body, headers, remote_ip) -> bool:
        # ЮKassa не подписывает уведомления — безопасность строится на
        # проверке IP-источника и последующем подтверждении статуса по API.
        if remote_ip is None:
            return False
        try:
            ip = ipaddress.ip_address(remote_ip)
        except ValueError:
            return False
        return any(ip in ipaddress.ip_network(net) for net in _YOOKASSA_NETS)

    def parse_webhook(self, payload: dict) -> dict:
        obj = payload.get("object", {})
        return {
            "provider_payment_id": obj.get("id"),
            "status": obj.get("status"),     # succeeded | canceled | ...
            "metadata": obj.get("metadata", {}),
        }


# ---------- Stripe (каркас) ----------

class StripeProvider(PaymentProvider):
    name = "stripe"

    def __init__(self) -> None:
        self._configured = bool(settings.stripe_secret_key)

    async def create_payment(self, *, amount_minor, currency, description,
                             return_url, metadata) -> PaymentCreated:
        # КАРКАС: здесь будет stripe.checkout.Session.create(...).
        # Возвращаемая структура совпадает с рабочим провайдером.
        if not self._configured:
            raise RuntimeError("Stripe не настроен (каркас, нужен stripe_secret_key)")
        raise NotImplementedError(
            "Stripe-провайдер — каркас. Подключите Stripe SDK здесь.")

    def verify_webhook(self, *, body, headers, remote_ip) -> bool:
        # КАРКАС: stripe.Webhook.construct_event(body, sig, webhook_secret).
        return False

    def parse_webhook(self, payload: dict) -> dict:
        return {"provider_payment_id": None, "status": None, "metadata": {}}


def get_provider(name: str) -> PaymentProvider:
    if name == "stripe":
        return StripeProvider()
    return YooKassaProvider()
