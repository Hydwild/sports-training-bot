"""Регрессии обёртки миграций, запускаемой из start.sh."""
import asyncio

from scripts import pre_migrate


def test_engine_is_disposed_in_the_same_event_loop(monkeypatch):
    loops = {}

    class FakeEngine:
        async def dispose(self):
            loops["dispose"] = asyncio.get_running_loop()

    async def fake_needs_stamp(engine):
        assert isinstance(engine, FakeEngine)
        loops["inspect"] = asyncio.get_running_loop()
        return False

    monkeypatch.setattr(pre_migrate, "needs_stamp", fake_needs_stamp)

    assert asyncio.run(pre_migrate._needs_stamp_and_dispose(FakeEngine())) is False
    assert loops["inspect"] is loops["dispose"]

