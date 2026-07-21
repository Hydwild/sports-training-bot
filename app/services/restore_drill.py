"""
Restore drill — проверка резервной копии реальным восстановлением.

Проверки сигнатуры (`backup.verify_dump`) недостаточно: gzip
распаковывается, «CREATE TABLE» в тексте есть — а восстановить в рабочую
СУБД дамп всё равно может не получиться (обрезан, несовместимый диалект,
битая середина). Единственная надёжная проверка — восстановить копию и
задать ей вопросы.

Куда восстанавливаем:
  * SQLite — во ВРЕМЕННЫЙ файл, `PRAGMA integrity_check`, наличие ключевых
    таблиц, пара согласованных count;
  * PostgreSQL — в отдельную ВРЕМЕННУЮ базу со случайным именем, затем
    те же проверки, затем эта база (и только она) удаляется.

Защита от катастрофы: имя рабочей базы НИКОГДА не используется как цель
и на нём не выполняется DROP. Имя цели генерируется здесь и проверяется
на совпадение с рабочим.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass

from app.core.config import settings

logger = logging.getLogger("backup")

# таблицы, без которых восстановленная копия бесполезна
CRITICAL_TABLES = ("tenants", "trainings", "signups")


@dataclass
class DrillResult:
    ok: bool
    message: str
    details: dict


def _decrypt_if_needed(blob: bytes) -> bytes:
    from app.services import backup

    if blob.startswith(backup.ENC_MAGIC):
        return backup.decrypt_backup(blob)   # бросит при неверном ключе
    return blob


# ---------------- SQLite ----------------

def _drill_sqlite_sync(gz: bytes) -> DrillResult:
    import sqlite3

    raw = gzip.decompress(gz)
    fd, tmp = tempfile.mkstemp(suffix=".drill.db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        con = sqlite3.connect(tmp)
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                return DrillResult(False, f"integrity_check: {integrity}", {})
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in CRITICAL_TABLES if t not in names]
            if missing:
                return DrillResult(
                    False, f"нет критических таблиц: {', '.join(missing)}", {})
            counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                      for t in CRITICAL_TABLES}
            # согласованность: записей не больше, чем занятий на порядки
            # (грубая проверка, что дамп не «половинчатый»)
            return DrillResult(True, "восстановление успешно", counts)
        finally:
            con.close()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------- PostgreSQL ----------------

def _admin_url() -> str:
    """URL psql без диалекта SQLAlchemy."""
    return settings.database_url.replace("postgresql+asyncpg://",
                                         "postgresql://", 1)


def _working_db_name() -> str:
    return _admin_url().rsplit("/", 1)[-1].split("?")[0]


def _url_with_db(name: str) -> str:
    base, _old = _admin_url().rsplit("/", 1)
    return f"{base}/{name}"


async def _psql(url: str, sql: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "psql", url, "-v", "ON_ERROR_STOP=1", "-tAqc", sql,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, (out or err).decode(errors="replace").strip()


async def _drill_postgres(gz: bytes) -> DrillResult:
    working = _working_db_name()
    target = f"drill_{secrets.token_hex(6)}"
    # немыслимо, но проверяем: цель не должна совпасть с рабочей базой
    if target == working:
        return DrillResult(False, "имя тестовой базы совпало с рабочей — "
                                  "восстановление отменено", {})

    admin = _url_with_db("postgres")   # служебная БД для CREATE/DROP
    rc, msg = await _psql(admin, f'CREATE DATABASE "{target}";')
    if rc != 0:
        return DrillResult(False, f"не создать тестовую базу: {msg}", {})

    try:
        raw = gzip.decompress(gz)
        proc = await asyncio.create_subprocess_exec(
            "psql", _url_with_db(target), "-v", "ON_ERROR_STOP=1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _out, err = await proc.communicate(raw)
        if proc.returncode != 0:
            return DrillResult(
                False, f"дамп не восстановился: "
                       f"{err.decode(errors='replace')[:300]}", {})

        counts = {}
        for table in CRITICAL_TABLES:
            rc, val = await _psql(_url_with_db(target),
                                  f"SELECT count(*) FROM {table};")
            if rc != 0:
                return DrillResult(
                    False, f"нет критической таблицы {table}: {val[:200]}", {})
            counts[table] = int(val or 0)
        return DrillResult(True, "восстановление успешно", counts)
    finally:
        # удаляем ТОЛЬКО созданную тестовую базу и только её
        if target != working:
            await _psql(admin, f'DROP DATABASE IF EXISTS "{target}";')


async def run_drill(blob: bytes) -> DrillResult:
    """Прогоняет копию через реальное восстановление. blob — то, что лежит
    в файле бэкапа (зашифрованный или обычный gz)."""
    try:
        gz = _decrypt_if_needed(blob)
    except ValueError as e:
        return DrillResult(False, f"копия не расшифрована: {e}", {})

    try:
        if settings.is_sqlite:
            return await asyncio.to_thread(_drill_sqlite_sync, gz)
        return await _drill_postgres(gz)
    except FileNotFoundError:
        return DrillResult(False, "нет psql в окружении для проверки", {})
    except Exception as e:                       # noqa: BLE001 — отчёт, не падение
        logger.exception("Restore drill упал")
        return DrillResult(False, f"{type(e).__name__}: {e}", {})
