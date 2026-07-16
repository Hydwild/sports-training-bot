"""
Конфигурация приложения (pydantic-settings).
Все значения читаются из переменных окружения или .env.

Главное переключение SQLite/PostgreSQL — через DATABASE_URL:
  Локально (по умолчанию): sqlite+aiosqlite:///./badminton.db
  Прод:                    postgresql+asyncpg://user:pass@host:5432/dbname
"""
import re
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_JWT_SECRET = "dev-insecure-change-me"
MIN_JWT_SECRET_LEN = 16  # минимальная длина боевого JWT-секрета
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?")


def tenant_suspended(tenant) -> bool:
    """SaaS: клуб приостановлен, если задана paid_until и дата прошла."""
    import datetime as _dt
    pu = (getattr(tenant, "paid_until", "") or "").strip()
    return bool(pu) and pu < _dt.date.today().isoformat()


def safe_color(value: str | None, default: str = "#3a7bd5") -> str:
    """Разрешаем только #RGB / #RRGGBB — защита от внедрения в <style>."""
    v = (value or "").strip()
    return v if _COLOR_RE.fullmatch(v) else default


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Редакция: lite (один тренер) | pro (клуб) ---
    edition: str = "pro"

    # --- База данных ---
    database_url: str = "sqlite+aiosqlite:///./badminton.db"

    # --- FastAPI ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # Базовый публичный URL (для построения webhook-адресов), напр. https://bot.example.com
    public_base_url: str = ""
    # Секрет для защиты служебных эндпойнтов API (заголовок X-Admin-Token)
    admin_api_token: str = ""
    # Секрет для подписи JWT (обязательно сменить в проде!)
    jwt_secret: str = INSECURE_JWT_SECRET

    # --- Telegram / VK (глобальные креды площадки; тенанты различаются внутри) ---
    tg_token: str = ""
    # Секретный токен Telegram webhook (X-Telegram-Bot-Api-Secret-Token)
    tg_webhook_secret: str = ""
    vk_token: str = ""
    vk_confirmation: str = ""   # строка подтверждения VK Callback API
    vk_secret: str = ""         # секрет VK Callback API

    # --- Запуск ботов ---
    # polling | webhook — режим Telegram
    tg_mode: str = "polling"
    run_vk_polling: bool = False

    # --- Админка ---
    tg_bot_username: str = ""        # username бота для Telegram Login Widget
    admin_dev_login: bool = False    # dev-вход без Telegram (только для отладки!)

    # --- Прочее ---
    timezone: str = "Europe/Moscow"
    log_dir: str = "logs"
    # SaaS: Telegram ID владельца площадки — получает алерты об истекающей
    # оплате клубов (см. tasks._daily_maintenance). Пусто — алерты не шлются.
    platform_owner_tg_id: int = 0

    # --- Платежи ---
    # ЮKassa (рабочий провайдер)
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    # Stripe (каркас)
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_pro(self) -> bool:
        return self.edition.lower() == "pro"

    @property
    def is_lite(self) -> bool:
        return not self.is_pro

    @property
    def is_insecure_jwt(self) -> bool:
        # небезопасно: дефолт, пусто или слишком короткий (легко подобрать)
        return (self.jwt_secret == INSECURE_JWT_SECRET
                or len(self.jwt_secret) < MIN_JWT_SECRET_LEN)

    def assert_production_secrets(self) -> None:
        """Фейл на старте, если в боевой конфигурации остались небезопасные
        дефолты. Разрешаем дефолт только в явном dev-режиме (ADMIN_DEV_LOGIN)."""
        if self.admin_dev_login:
            return
        problems = []
        if self.is_insecure_jwt:
            problems.append(
                "JWT_SECRET пустой, дефолтный или короче "
                f"{MIN_JWT_SECRET_LEN} символов — задайте случайную строку "
                "(например, `openssl rand -base64 32`)")
        if self.tg_mode == "webhook" and not self.tg_webhook_secret:
            problems.append("TG_MODE=webhook, но TG_WEBHOOK_SECRET не задан")
        if problems:
            raise RuntimeError(
                "Небезопасная конфигурация, старт остановлен:\n  - "
                + "\n  - ".join(problems)
                + "\n(в локальной отладке выставьте ADMIN_DEV_LOGIN=true)")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
