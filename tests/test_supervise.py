"""
tasks.supervise: автоперезапуск фоновых задач (run_polling ботов).

Регресс: раньше если Long Poll (TG/VK) падал с необработанным исключением,
бот молча замолкал для всех клиентов до ручного рестарта — ни ретрая,
ни алерта. Теперь supervise() перезапускает с бэкоффом и алертит
владельца площадки после нескольких падений подряд.
"""
import asyncio

import pytest

from app.services import tasks


async def test_clean_return_no_retry():
    """Штатное завершение (например, токен бота не настроен) — не крашер,
    перезапускать не нужно."""
    calls = {"n": 0}

    async def clean():
        calls["n"] += 1

    await tasks.supervise("test-clean", clean, base_backoff=0)
    assert calls["n"] == 1


async def test_retries_after_failure_then_recovers():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("временный сбой сети")

    await tasks.supervise("test-flaky", flaky, base_backoff=0)
    assert calls["n"] == 2  # упал один раз, перезапустился, дальше ок


async def test_alerts_owner_after_three_failures_in_a_row(monkeypatch):
    alerts = []

    async def fake_alert(where, err):
        alerts.append((where, str(err)))

    monkeypatch.setattr(tasks, "_alert_admins", fake_alert)

    calls = {"n": 0}

    async def persistent():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError(f"сбой №{calls['n']}")

    await tasks.supervise("test-persistent", persistent, base_backoff=0)
    assert calls["n"] == 4          # 3 провала + успешная 4-я попытка
    assert len(alerts) == 1         # алерт ровно один раз, не на каждый провал
    assert alerts[0][0] == "test-persistent"


async def test_telegram_run_polling_propagates_crash():
    """Регресс: раньше если start_polling падал НЕ из-за reload (реальный
    сбой сети/API), run_polling() тихо возвращался без исключения —
    supervise() не мог понять, что бот упал, и не перезапускал его."""
    import app.bots.telegram as tg

    class FakeDispatcher:
        async def start_polling(self, *bots):
            raise RuntimeError("сеть упала")

    old_bot, old_dp = tg._bot, tg._dp
    tg._bot, tg._dp = object(), FakeDispatcher()
    try:
        with pytest.raises(RuntimeError, match="сеть упала"):
            await tg.run_polling()
    finally:
        tg._bot, tg._dp = old_bot, old_dp


async def test_cancellation_propagates_not_retried():
    """CancelledError — это остановка задачи (например, при shutdown), а не
    сбой; не должна попадать в цикл ретраев."""
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.sleep(100)

    task = asyncio.create_task(tasks.supervise("test-cancel", forever, base_backoff=0))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
