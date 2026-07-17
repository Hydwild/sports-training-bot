"""
_poll_forever в vk.py: безопасный обход LoopWrapper из vkbottle.

Регресс: bot.run_polling() из vkbottle проверяет свой ВНУТРЕННИЙ флаг
loop_wrapper.is_running (не сам event loop) и при первом вызове всегда
пытается сам запустить/остановить event loop через синхронный
LoopWrapper.run() — тот падает с RuntimeError, если вызван из уже
работающего loop'а (наш случай, мы всегда внутри loop'а FastAPI/uvicorn).
Раньше исключение молча терялось (task exception was never retrieved), а
внутренний поллинг-таск, который add_task() успевал запланировать ДО
падения, оставался работать "осиротевшим". После того как run_polling()
попал под tasks.supervise() (авто-ретрай), каждый повторный вызов после
падения добавлял ЕЩЁ один осиротевший поллинг-таск поверх предыдущих —
отсюда дублирующиеся ответы бота (несколько параллельных Long Poll на
один и тот же бот). _poll_forever обходит LoopWrapper полностью.
"""
import asyncio

from app.bots.vk import _poll_forever


async def test_poll_forever_dispatches_without_loop_wrapper():
    routed = []

    class FakePollingObj:
        api = "fake-api"

        async def listen(self):
            yield {"updates": [{"id": 1}, {"id": 2}]}

    class FakeRouter:
        async def route(self, update, api):
            routed.append((update, api))

    class FakeBot:
        # намеренно нет loop_wrapper — если бы код обращался к нему,
        # тест упал бы с AttributeError, подтверждая, что LoopWrapper
        # (источник исходного RuntimeError) больше не используется
        @property
        def polling(self):
            return FakePollingObj()

        @property
        def router(self):
            return FakeRouter()

    await _poll_forever(FakeBot())
    await asyncio.sleep(0)  # дать созданным create_task диспетчеризацию выполниться

    assert len(routed) == 2
    assert routed[0] == ({"id": 1}, "fake-api")
    assert routed[1] == ({"id": 2}, "fake-api")


async def test_poll_forever_retries_are_idempotent_no_duplicate_dispatch():
    """Повторный вызов (как делает supervise() при ретрае после падения)
    не должен накапливать состояние — каждый вызов независим и не плодит
    лишних диспетчеризаций сверх тех, что реально пришли в этом вызове."""
    routed = []
    call_count = {"n": 0}

    class FakePollingObj:
        api = "fake-api"

        async def listen(self):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("временный сбой сети")
            yield {"updates": [{"id": 99}]}

    class FakeRouter:
        async def route(self, update, api):
            routed.append(update)

    class FakeBot:
        @property
        def polling(self):
            return FakePollingObj()

        @property
        def router(self):
            return FakeRouter()

    from app.services import tasks
    bot = FakeBot()
    await tasks.supervise("test-vk-poll", lambda: _poll_forever(bot), base_backoff=0)
    await asyncio.sleep(0)

    assert call_count["n"] == 2       # упал один раз, восстановился
    assert routed == [{"id": 99}]     # ровно одна диспетчеризация, без дублей
