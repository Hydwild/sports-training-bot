"""
Внешние бэкапы базы (app/services/backup.py): дамп + отправка владельцу
площадки в Telegram, вне инфраструктуры Railway. Реальный pg_dump/Telegram
не вызываются — подменяем через monkeypatch.
"""
import gzip

from app.core.config import settings
from app.services import backup


async def test_no_owner_id_returns_clear_message(monkeypatch):
    monkeypatch.setattr(settings, "platform_owner_tg_id", 0)
    result = await backup.send_backup_to_owner()
    assert "PLATFORM_OWNER_TG_ID" in result


async def test_dump_failure_returns_clear_message(monkeypatch):
    monkeypatch.setattr(settings, "platform_owner_tg_id", 12345)

    async def fake_make_dump():
        return None

    monkeypatch.setattr(backup, "_make_dump", fake_make_dump)
    result = await backup.send_backup_to_owner()
    assert "Не удалось создать дамп" in result


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
    assert "МБ" in result and "не отправлен" in result
    assert sent["called"] is False


async def test_successful_backup_sends_document(monkeypatch):
    monkeypatch.setattr(settings, "platform_owner_tg_id", 12345)

    async def fake_make_dump():
        return b"fake dump content", "backup_2026-01-01.sql.gz"

    monkeypatch.setattr(backup, "_make_dump", fake_make_dump)

    calls = []

    async def fake_send(user_id, filename, data, caption=""):
        calls.append((user_id, filename, data, caption))
        return True

    import app.bots.telegram as tg
    monkeypatch.setattr(tg, "send_document_to_owner", fake_send)

    result = await backup.send_backup_to_owner()
    assert "отправлен" in result
    assert len(calls) == 1
    assert calls[0][0] == 12345
    assert calls[0][1] == "backup_2026-01-01.sql.gz"
    assert calls[0][2] == b"fake dump content"


async def test_send_failure_reported(monkeypatch):
    monkeypatch.setattr(settings, "platform_owner_tg_id", 12345)

    async def fake_make_dump():
        return b"data", "backup.sql.gz"

    monkeypatch.setattr(backup, "_make_dump", fake_make_dump)

    async def fake_send(*a, **kw):
        return False

    import app.bots.telegram as tg
    monkeypatch.setattr(tg, "send_document_to_owner", fake_send)

    result = await backup.send_backup_to_owner()
    assert "не удалось" in result


# ---------- pg_dump: успех/ошибка через мок subprocess ----------

class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


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
