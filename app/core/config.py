"""
Конфигурация приложения (pydantic-settings).
Все значения читаются из переменных окружения или .env.

Главное переключение SQLite/PostgreSQL — через DATABASE_URL:
  Локально (по умолчанию): sqlite+aiosqlite:///./badminton.db
  Прод:                    postgresql+asyncpg://user:pass@host:5432/dbname
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    jwt_secret: str = "dev-insecure-change-me"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
