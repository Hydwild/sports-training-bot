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
    # Ключ шифрования телефонов веб-клиентов (см. app/core/phones.py),
    # версия v1. Пусто — используется исторический ключ, выведенный из
    # jwt_secret (версия jwt).
    phone_enc_key: str = ""
    # Связка прежних ключей телефонов на время перехода: `ver:secret,...`.
    # Нужна, чтобы добавление PHONE_ENC_KEY и последующая ротация
    # JWT_SECRET не сделали старые номера нечитаемыми и не наплодили
    # дублей клиентов. После migrate_phone_keys --apply --verify связку
    # можно убрать.
    phone_keyring: str = ""
    # Именованные НЕИЗМЕНЯЕМЫЕ версии ключей телефонов: `v1:secret,v2:secret`.
    # Предпочтительный способ вместо PHONE_ENC_KEY/PHONE_KEYRING (они
    # остаются для обратной совместимости). Версия — произвольная метка,
    # которая НИКОГДА не меняет смысл: под ней зашифрованы конкретные строки.
    phone_keys: str = ""
    # Какой версией шифровать и индексировать НОВЫЕ записи (напр. "v2").
    # Пусто — обратно совместимый выбор: v1 при заданном PHONE_ENC_KEY,
    # иначе исторический jwt.
    phone_active_key_version: str = ""
    # Версии, которые МОГУТ присутствовать в данных и обязаны быть читаемы.
    # Если хоть одна из них недоступна (нет ключа) — новые клиенты НЕ
    # создаются: под нечитаемым индексом мог бы уже лежать этот телефон, и
    # мы бы завели дубль. Пусто — ожидается только историческая jwt.
    phone_legacy_versions: str = ""
    # Ключ шифрования токенов Telegram/VK клубов (см. app/core/bot_tokens.py).
    # Отдельный от JWT и от ключа телефонов: разные сроки жизни и разные
    # последствия компрометации. Обязателен для Pro после миграции токенов.
    bot_token_enc_key: str = ""
    # Связка прежних ключей токенов на время замены: `ver:secret,...`
    bot_token_keyring: str = ""
    # Именованные версии ключей токенов (аналог phone_keys): `v1:secret,...`
    bot_token_keys: str = ""
    # Какой версией шифровать новые токены. Пусто — v1.
    bot_token_active_key_version: str = ""
    # Ключ шифрования резервных копий (см. app/services/backup.py). Копия
    # уходит в Telegram — без шифрования это отправка всей базы в чат.
    # Ключ НЕ должен храниться там же, где копии.
    backup_enc_key: str = ""
    # Кому доверять заголовок X-Forwarded-For. Передаётся uvicorn как
    # --forwarded-allow-ips (см. start.sh). Пусто — не доверять никому
    # (безопасный умолчательный режим: подделать адрес нельзя, но за
    # прокси все посетители выглядят одинаково и лимит станет общим).
    # На Railway/Render, где адрес прокси динамический, ставят "*".
    trusted_proxies: str = ""

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
    # SaaS: контакт для продления оплаты, подставляется в уведомление клиенту
    # (например "@ваш_ник" или номер телефона). Пусто — общая формулировка.
    platform_support_contact: str = ""

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

    def public_url(self, path: str) -> str:
        """Абсолютный публичный URL для пути вида '/club/1/m/xyz'.

        Нужен там, где ссылку КОПИРУЮТ и отправляют вовне — например
        администратор выдаёт клиенту ссылку управления: относительный
        '/club/...' в переписке/мессенджере не откроется.

        «Безопасно» — значит берём только заранее заданный владельцем
        PUBLIC_BASE_URL и только схему http(s); host из запроса (заголовок
        Host, который клиент контролирует) не подставляем. Пустой или иной
        схемы base — ошибка конфигурации, а не молчаливый относительный путь:
        лучше явно попросить владельца задать PUBLIC_BASE_URL, чем выдать
        клиенту нерабочую ссылку."""
        base = (self.public_base_url or "").strip().rstrip("/")
        if not base or not re.match(r"^https?://", base, re.IGNORECASE):
            raise RuntimeError(
                "PUBLIC_BASE_URL не задан или не http(s) — не могу построить "
                "абсолютную ссылку. Задайте PUBLIC_BASE_URL (например "
                "https://bot.example.com) и повторите.")
        if not path.startswith("/"):
            path = "/" + path
        return base + path

    @property
    def in_proxy_env(self) -> bool:
        """Признак, что приложение развёрнуто ЗА обратным прокси, где адрес
        соединения — это прокси, а реальный клиент в X-Forwarded-For.
        Определяем по маркерам платформы, а не гадаем: Railway и Render
        выставляют собственные переменные окружения."""
        import os
        markers = ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID",
                   "RAILWAY_SERVICE_ID", "RENDER", "RENDER_SERVICE_ID")
        return any(os.environ.get(m) for m in markers)

    @property
    def proxy_headers_configured(self) -> bool:
        """Задан ли доверенный список прокси (uvicorn --forwarded-allow-ips).
        Безопасный boolean для /health — без раскрытия самих адресов."""
        return bool((self.trusted_proxies or "").strip())

    def assert_proxy_config(self) -> None:
        """Fail-fast: в явной proxy-среде (Railway/Render) без TRUSTED_PROXIES
        все посетители за прокси делят один адрес, и общий rate limit снова
        становится общим на всех — это ровно тот боевой дефект, что уже
        чинили. В dev-режиме не мешаем."""
        if self.admin_dev_login:
            return
        if self.in_proxy_env and not self.proxy_headers_configured:
            raise RuntimeError(
                "Приложение развёрнуто за обратным прокси (Railway/Render), "
                "но TRUSTED_PROXIES не задан. Без него X-Forwarded-For не "
                "применяется, и все посетители за прокси делят один адрес и "
                "общий rate limit. Задайте TRUSTED_PROXIES=* (только если "
                "прямой доступ к контейнеру закрыт) и перезапустите.")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
