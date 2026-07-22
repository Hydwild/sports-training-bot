"""
Деплой не должен зависеть от доступности сторонних сервисов и не должен
уметь зависать без объяснения.

Два боевых урока:
  * зависший деплой Railway (start.sh не доходил до uvicorn) — теперь
    ожидание блокировки в миграциях ограничено по времени;
  * авторегистрация webhook на старте валила деплой, если Telegram
    ответил ошибкой. При перезапуске в crash-loop это самоподдерживающийся
    отказ: каждый старт дёргает setWebhook, ловит 429 и падает снова.
"""
import pytest

from app.bots import telegram as tg
from app.core.config import settings


@pytest.fixture(autouse=True)
def _reset_sync_flag(monkeypatch):
    monkeypatch.setattr(tg, "_global_delivery_synced", False)
    yield


class _Bot:
    """Заглушка Bot API: считает вызовы и умеет падать."""

    def __init__(self, *, fail: Exception | None = None, ok: bool = True):
        self.fail = fail
        self.ok = ok
        self.set_calls = 0
        self.delete_calls = 0

    async def set_webhook(self, **kwargs):
        self.set_calls += 1
        if self.fail:
            raise self.fail
        return self.ok

    async def delete_webhook(self, **kwargs):
        self.delete_calls += 1
        if self.fail:
            raise self.fail
        return self.ok


# ---------- Telegram недоступен: старт продолжается ----------

async def test_telegram_outage_does_not_break_startup(monkeypatch):
    """Сетевой сбой Telegram — не повод не подняться."""
    bot = _Bot(fail=ConnectionError("telegram unreachable"))
    monkeypatch.setattr(tg, "_bot", bot)
    monkeypatch.setattr(settings, "tg_mode", "polling")

    assert await tg.configure_global_delivery() is False
    assert tg.global_delivery_synced() is False      # видно в /health


async def test_rate_limit_does_not_break_startup(monkeypatch):
    """429 при перезапуске в crash-loop раньше делал отказ вечным."""
    bot = _Bot(fail=RuntimeError("Too Many Requests: retry after 30"))
    monkeypatch.setattr(tg, "_bot", bot)
    monkeypatch.setattr(settings, "tg_mode", "polling")

    assert await tg.configure_global_delivery() is False


async def test_api_refusal_does_not_break_startup(monkeypatch):
    """Telegram ответил, но не подтвердил — тоже не валим старт."""
    monkeypatch.setattr(tg, "_bot", _Bot(ok=False))
    monkeypatch.setattr(settings, "tg_mode", "polling")

    assert await tg.configure_global_delivery() is False


async def test_success_marks_synced(monkeypatch):
    bot = _Bot()
    monkeypatch.setattr(tg, "_bot", bot)
    monkeypatch.setattr(settings, "tg_mode", "polling")

    assert await tg.configure_global_delivery() is True
    assert tg.global_delivery_synced() is True
    assert bot.delete_calls == 1        # перед polling webhook снимается


async def test_webhook_mode_registers_url(monkeypatch):
    bot = _Bot()
    monkeypatch.setattr(tg, "_bot", bot)
    monkeypatch.setattr(settings, "tg_mode", "webhook")
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example.com")

    assert await tg.configure_global_delivery() is True
    assert bot.set_calls == 1


# ---------- ошибки конфигурации по-прежнему валят старт ----------

async def test_non_https_base_url_still_fails_fast(monkeypatch):
    """Детерминированная ошибка: сама не пройдёт, в фоне повторять нечего."""
    bot = _Bot()
    monkeypatch.setattr(tg, "_bot", bot)
    monkeypatch.setattr(settings, "tg_mode", "webhook")
    monkeypatch.setattr(settings, "public_base_url", "http://bot.example.com")

    with pytest.raises(RuntimeError):
        await tg.configure_global_delivery()
    assert bot.set_calls == 0, "к Telegram не ходим с заведомо плохим URL"


async def test_unknown_mode_still_fails_fast(monkeypatch):
    bot = _Bot()
    monkeypatch.setattr(tg, "_bot", bot)
    monkeypatch.setattr(settings, "tg_mode", "carrier-pigeon")

    with pytest.raises(RuntimeError):
        await tg.configure_global_delivery()
    assert bot.set_calls == 0 and bot.delete_calls == 0


# ---------- фоновый дотяг ----------

async def test_background_retry_eventually_succeeds(monkeypatch):
    """Приложение поднялось немым — фоновая задача дотягивает режим."""
    calls = {"n": 0}

    async def flaky() -> bool:
        calls["n"] += 1
        if calls["n"] < 3:
            return False
        monkeypatch.setattr(tg, "_global_delivery_synced", True)
        return True

    monkeypatch.setattr(tg, "configure_global_delivery", flaky)
    await tg.sync_global_delivery_loop(delay=0, max_delay=0)
    assert calls["n"] == 3


async def test_background_retry_stops_on_config_error(monkeypatch):
    """Конфигурационную ошибку не долбим бесконечно — Telegram лимитирует."""
    calls = {"n": 0}

    async def broken() -> bool:
        calls["n"] += 1
        raise RuntimeError("PUBLIC_BASE_URL должен начинаться с https://")

    monkeypatch.setattr(tg, "configure_global_delivery", broken)
    await tg.sync_global_delivery_loop(delay=0, max_delay=0)
    assert calls["n"] == 1


# ---------- миграции не ждут блокировку вечно ----------

def test_migrations_bound_the_lock_wait():
    """Без lock_timeout ALTER TABLE ждёт блокировку бесконечно: start.sh не
    доходит до uvicorn и деплой висит без единой строки в логах."""
    from app.db.migration_settings import lock_timeout

    assert lock_timeout() == "10s"


def test_lock_timeout_is_configurable(monkeypatch):
    from app.db.migration_settings import lock_timeout

    monkeypatch.setenv("MIGRATION_LOCK_TIMEOUT", "45s")
    assert lock_timeout() == "45s"


@pytest.mark.parametrize("bad", ["10s; DROP TABLE tenants", "', '1", "abc",
                                 "", "10 s", "-5s"])
def test_lock_timeout_rejects_injection(monkeypatch, bad):
    """Значение подставляется в текст SET-запроса, поэтому мусор отбрасываем."""
    from app.db.migration_settings import lock_timeout

    monkeypatch.setenv("MIGRATION_LOCK_TIMEOUT", bad)
    assert lock_timeout() == "10s"


def test_lock_timeout_applied_only_to_postgres():
    """SQLite такого параметра не знает — там он не задаётся.

    Задавать его надо ПАРАМЕТРОМ СОЕДИНЕНИЯ, а не отдельным SET внутри
    транзакции миграции: лишний запрос в той же транзакции ломал
    `alembic check` на PostgreSQL (CI это и поймал).

    env.py читаем как текст: импортировать его нельзя, он на импорте
    немедленно запускает миграции."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "alembic" / "env.py"
    text = src.read_text(encoding="utf-8")
    assert '"asyncpg" in settings.database_url' in text
    assert '"lock_timeout": lock_timeout()' in text
    assert "server_settings" in text
    assert "from app.db.migration_settings import lock_timeout" in text
    # регресс: SET внутри транзакции миграции больше не выполняем
    assert "SET lock_timeout" not in text
