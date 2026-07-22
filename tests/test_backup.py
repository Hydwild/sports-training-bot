"""
Внешние бэкапы базы (app/services/backup.py): дамп + отправка владельцу
площадки в Telegram, вне инфраструктуры Railway. Реальный pg_dump/Telegram
не вызываются — подменяем через monkeypatch.
"""
import gzip

from app.core.config import settings
from app.services import backup, tasks
from app.services.backup import BackupResult


async def test_no_owner_id_returns_clear_message(monkeypatch):
    monkeypatch.setattr(settings, "platform_owner_tg_id", 0)
    result = await backup.send_backup_to_owner()
    assert not result.ok and "PLATFORM_OWNER_TG_ID" in result.message


async def test_dump_failure_returns_clear_message(monkeypatch):
    monkeypatch.setattr(settings, "platform_owner_tg_id", 12345)

    async def fake_make_dump():
        return None

    monkeypatch.setattr(backup, "_make_dump", fake_make_dump)
    result = await backup.send_backup_to_owner()
    assert not result.ok and "Не удалось создать дамп" in result.message


def _valid_dump() -> bytes:
    """Правдоподобный архив: копия проверяется на содержимое перед
    отправкой (см. backup.verify_dump), пустышка до Telegram не доедет."""
    import gzip
    if settings.is_sqlite:                     # в тестах база — SQLite
        body = b"SQLite format 3\x00" + b"\x00" * 4000
    else:
        body = (b"-- PostgreSQL database dump\n"
                b"CREATE TABLE public.tenants (id integer);\n" + b"-" * 4000)
    return gzip.compress(body)


async def test_oversized_dump_not_sent(monkeypatch):
    monkeypatch.setattr(settings, "platform_owner_tg_id", 12345)

    async def fake_make_dump():
        # больше лимита MAX_TELEGRAM_FILE_MB, без реального большого файла
        return b"x" * 1024, "backup_test.sql.gz"

    monkeypatch.setattr(backup, "_make_dump", fake_make_dump)
    monkeypatch.setattr(backup, "MAX_TELEGRAM_FILE_MB", 0)  # любой файл "слишком большой"

    sent = {"called": False}

    async def fake_send(*a, **kw):
        sent["called"] = True
        return True

    import app.bots.telegram as tg
    monkeypatch.setattr(tg, "send_document_to_owner", fake_send)

    result = await backup.send_backup_to_owner()
    assert not result.ok
    assert "МБ" in result.message and "не отправлен" in result.message
    assert sent["called"] is False


async def test_successful_backup_sends_document(monkeypatch):
    monkeypatch.setattr(settings, "platform_owner_tg_id", 12345)

    async def fake_make_dump():
        return _valid_dump(), "backup_2026-01-01.sql.gz"

    monkeypatch.setattr(backup, "_make_dump", fake_make_dump)

    calls = []

    async def fake_send(user_id, filename, data, caption=""):
        calls.append((user_id, filename, data, caption))
        return True

    import app.bots.telegram as tg
    monkeypatch.setattr(tg, "send_document_to_owner", fake_send)

    result = await backup.send_backup_to_owner()
    assert result.ok and "отправлен" in result.message
    assert len(calls) == 1
    assert calls[0][0] == 12345
    # копия уходит зашифрованной, поэтому имя с .enc, а содержимое — не дамп
    assert calls[0][1] == "backup_2026-01-01.sql.gz.enc"
    assert calls[0][2] != _valid_dump()
    assert backup.decrypt_backup(calls[0][2]) == _valid_dump()
    assert backup.checksum(calls[0][2]) in calls[0][3]   # сумма отправленного


async def test_send_failure_reported(monkeypatch):
    monkeypatch.setattr(settings, "platform_owner_tg_id", 12345)

    async def fake_make_dump():
        return _valid_dump(), "backup.sql.gz"

    monkeypatch.setattr(backup, "_make_dump", fake_make_dump)

    async def fake_send(*a, **kw):
        return False

    import app.bots.telegram as tg
    monkeypatch.setattr(tg, "send_document_to_owner", fake_send)

    result = await backup.send_backup_to_owner()
    assert not result.ok and "не удалось" in result.message


# ---------- pg_dump: успех/ошибка через мок subprocess ----------

class _FakeStream:
    """Асинхронный поток поверх bytes: pg_dump читается кусками, а не
    целиком через communicate() — дамп не должен полностью попадать в RAM."""

    def __init__(self, data: bytes):
        import io as _io
        self._buf = _io.BytesIO(data)

    async def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int):
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.returncode = returncode

    async def wait(self):
        return self.returncode


async def test_pg_dump_success_gzips_output(monkeypatch):
    async def fake_exec(*args, **kwargs):
        assert args[0] == "pg_dump"
        return _FakeProcess(b"-- sql dump content --", b"", 0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    result = await backup._dump_postgres()
    assert result is not None
    data, name = result
    assert name.startswith("backup_") and name.endswith(".sql.gz")
    assert gzip.decompress(data) == b"-- sql dump content --"


async def test_pg_dump_nonzero_exit_returns_none(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProcess(b"", b"connection refused", 1)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    assert await backup._dump_postgres() is None


async def test_pg_dump_binary_missing_returns_none(monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError("pg_dump: command not found")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    assert await backup._dump_postgres() is None


def test_pg_dump_url_strips_asyncpg_dialect(monkeypatch):
    monkeypatch.setattr(settings, "database_url",
                        "postgresql+asyncpg://user:pass@host:5432/db")
    assert backup._pg_dump_url() == "postgresql://user:pass@host:5432/db"


# ---------- SQLite: реальный временный файл ----------

def test_dump_sqlite_sync_produces_valid_gzip(tmp_path, monkeypatch):
    import sqlite3
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_path}")
    result = backup._dump_sqlite_sync()
    assert result is not None
    data, name = result
    assert name.startswith("backup_") and name.endswith(".db.gz")
    raw = gzip.decompress(data)
    assert raw[:16] == b"SQLite format 3\x00"


def test_dump_sqlite_missing_file_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "database_url",
                        "sqlite+aiosqlite:///./does_not_exist_xyz.db")
    assert backup._dump_sqlite_sync() is None


# ---------- Регресс: день бэкапа помечается только после попытки ----------

async def test_offsite_backup_marks_day_only_after_attempt(monkeypatch):
    """Раньше last_day помечался ДО вызова send_backup_to_owner — необработанное
    исключение внутри него всё равно "съедало" сегодняшнюю попытку и откладывало
    следующую на завтра. Теперь при падении last_day должен остаться прежним."""
    async def fake_send_raises():
        raise RuntimeError("boom")

    monkeypatch.setattr(backup, "send_backup_to_owner", fake_send_raises)
    last_day = [None]
    try:
        await tasks._offsite_backup(last_day)
        raise AssertionError("ожидалось исключение")
    except RuntimeError:
        pass
    assert last_day[0] is None  # день НЕ помечен — повтор возможен на следующем проходе


async def test_offsite_backup_marks_day_after_successful_attempt(monkeypatch):
    async def fake_send_ok():
        return BackupResult(True, "Бэкап отправлен: ok")

    monkeypatch.setattr(backup, "send_backup_to_owner", fake_send_ok)
    last_day = [None]
    await tasks._offsite_backup(last_day)
    import datetime as dt
    assert last_day[0] == dt.date.today().isoformat()
