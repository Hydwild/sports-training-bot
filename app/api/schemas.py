"""Pydantic-схемы запросов/ответов API."""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, field_validator


class TenantCreate(BaseModel):
    name: str
    tg_chat_id: int | None = None
    vk_group_id: int | None = None
    admin_tg_id: int | None = None
    admin_vk_id: int | None = None
    timezone: str = "Europe/Moscow"
    is_demo: bool = False


class TenantOut(BaseModel):
    id: int
    name: str
    tg_chat_id: int | None
    admin_tg_id: int | None
    timezone: str
    is_active: bool
    is_demo: bool = False

    class Config:
        from_attributes = True


class TrainingCreate(BaseModel):
    title: str
    start_at: dt.datetime
    location: str = ""
    max_participants: int = Field(gt=0)
    duration_min: int = Field(default=120, gt=0)
    price_minor: int = Field(default=0, ge=0)
    currency: str = "RUB"
    state: str = "published"
    publish_at: dt.datetime | None = None


class MembershipSet(BaseModel):
    tg_user_id: int
    role: str  # owner | coach | assistant
    name: str = ""


class PaymentStart(BaseModel):
    training_id: int
    platform: str = "tg"
    user_id: int
    return_url: str

    @field_validator("return_url")
    @classmethod
    def _return_url_must_be_http(cls, v: str) -> str:
        """Эндпойнт уже защищён require_admin, но не полагаемся только на
        это: провайдер оплаты перенаправит пользователя по этому адресу
        после оплаты — ограничиваем схему, чтобы сюда нельзя было
        подсунуть javascript:/data: и т.п."""
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("return_url должен начинаться с http:// или https://")
        return v


class BrandUpdate(BaseModel):
    brand_name: str | None = None
    brand_color: str | None = None
    brand_logo_url: str | None = None
    payment_provider: str | None = None


class TrainingOut(BaseModel):
    id: int
    tenant_id: int
    title: str
    start_at: dt.datetime
    location: str
    max_participants: int
    duration_min: int
    state: str
    is_cancelled: bool

    class Config:
        from_attributes = True


class SignupOut(BaseModel):
    id: int
    name: str
    platform: str
    status: str
    position: int
    attended: bool
    paid: bool

    class Config:
        from_attributes = True
