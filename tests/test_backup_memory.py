"""
Бэкап не должен поднимать базу в память целиком.

Это не про красоту, а про счёт: Railway тарифицирует СРЕДНЮЮ память, а
Python не возвращает крупные освобождённые блоки ОС сразу — поэтому пик
суточного бэкапа оставался в RSS и оплачивался ещё много часов после.
Раньше одновременно жили: сырой дамп, его gzip-копия, ещё одна полная
копия при проверке и зашифрованный результат.
"""
import gzip
import io
import tracemalloc

import pytest

from app.services import backup

# «База» заметно больше куска потоковой обработки, но с текстом, который
# хорошо жмётся — так видно разницу между потоком и полной копией.
BIG_RAW = (b"-- PostgreSQL database dump\n"
           b"CREATE TABLE public.tenants (id integer);\n"
           + b"INSERT INTO public.tenants VALUES (1);\n" * 300_000)


class _FakeStream:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    async def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


class _FakeProcess:
    def __init__(self, stdout: bytes):
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(b"")
        self.returncode = 0

    async def wait(self):
        return 0


async def test_pg_dump_does_not_hold_whole_database_in_memory(monkeypatch):
    """Пик выделений при снятии дампа должен быть заметно меньше размера
    самой базы: сырой вывод pg_dump сжимается по мере чтения."""
    async def fake_exec(*args, **kwargs):
        return _FakeProcess(BIG_RAW)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    tracemalloc.start()
    try:
        result = await backup._dump_postgres()
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result is not None
    data, _name = result
    # содержимое не пострадало
    assert gzip.decompress(data) == BIG_RAW
    # пик существенно ниже размера «базы» — полной копии в памяти нет
    assert peak < len(BIG_RAW) / 2, (
        f"пик {peak} байт при размере базы {len(BIG_RAW)} — дамп всё ещё "
        "поднимается в память целиком")


def test_verify_does_not_decompress_whole_archive(monkeypatch):
    """verify_dump раньше делал gzip.decompress целиком — это была вторая
    полная копия базы рядом с архивом."""
    monkeypatch.setattr(type(backup.settings), "is_sqlite",
                        property(lambda self: False))
    archive = gzip.compress(BIG_RAW)

    tracemalloc.start()
    try:
        problem = backup.verify_dump(archive)
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert problem == ""                     # дамп валиден
    assert peak < len(BIG_RAW) / 2, (
        f"пик {peak} байт при размере базы {len(BIG_RAW)} — проверка всё "
        "ещё распаковывает архив целиком")


async def test_pg_dump_reads_stderr_and_survives_noisy_output(monkeypatch):
    """stderr вычитывается параллельно: иначе на большом выводе pg_dump
    заблокировался бы на переполненном пайпе, а бэкап завис бы навсегда."""
    class _Noisy(_FakeProcess):
        def __init__(self):
            super().__init__(b"")
            self.stderr = _FakeStream(b"WARNING: noisy\n" * 100_000)
            self.returncode = 1

    async def fake_exec(*args, **kwargs):
        return _Noisy()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    # ненулевой код возврата -> None, но без зависания
    assert await backup._dump_postgres() is None


@pytest.mark.parametrize("payload,expected", [
    (b"not a gzip archive", "не распаковывается"),
    (gzip.compress(b"tiny"), "мал"),
])
def test_verify_still_rejects_bad_archives(payload, expected):
    """Потоковая проверка не ослабила прежние отказы."""
    assert expected in backup.verify_dump(payload)


def test_release_memory_is_safe_everywhere():
    """malloc_trim есть не на всякой платформе (musl, Windows) — вызов
    обязан молча пройти, а не уронить бэкап."""
    backup._release_memory()
