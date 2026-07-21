"""
Резервное копирование базы (Postgres или SQLite) с отправкой владельцу
площадки в Telegram файлом — НЕ зависит от инфраструктуры Railway, так
что переживает полное падение платформы (аккаунт, биллинг, региональный
сбой и т.п.). См. DISASTER_RECOVERY.md для полного плана восстановления.

Вызывается:
  - раз в сутки автоматически (tasks.scheduler_loop -> _offsite_backup),
  - вручную из панели оператора (/admin/platform/backup-now).
"""
from __future__ import annotations

from dataclasses import dataclass

import asyncio
import datetime as dt
import gzip
import logging
import os

from app.core.config import settings

logger = logging.getLogger("backup")

# Telegram ограничивает файлы, отправляемые ботом, 50 МБ — оставляем запас
MAX_TELEGRAM_FILE_MB = 45


def _pg_dump_url() -> str:
    """DATABASE_URL для pg_dump: убираем SQLAlchemy-специфичный суффикс
    диалекта (+asyncpg) — pg_dump понимает только стандартный postgresql://."""
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _dump_postgres() -> tuple[bytes, str] | None:
    """pg_dump (обычный SQL, без владельцев/прав — переносимо на любой
    хостинг) -> gzip. None при ошибке (см. логи)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pg_dump", "--no-owner", "--no-acl", "--dbname", _pg_dump_url(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.error("pg_dump не найден в образе (нужен пакет postgresql-client)")
        return None
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error("pg_dump завершился с ошибкой (%s): %s",
                     proc.returncode, stderr.decode(errors="replace")[:500])
        return None
    if not stdout:
        logger.error("pg_dump вернул пустой дамп")
        return None
    data = gzip.compress(stdout)
    name = f"backup_{dt.date.today().isoformat()}.sql.gz"
    return data, name


def _sqlite_path() -> str | None:
    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    tail = url.split("///")[-1]
    return "/" + tail if url.count("/") >= 4 and not tail.startswith("/") else tail


def _dump_sqlite_sync() -> tuple[bytes, str] | None:
    """Горячая копия SQLite через backup API (консистентно при записи),
    затем сжимаем. Синхронная — вызывается через asyncio.to_thread."""
    import sqlite3
    import tempfile
    path = _sqlite_path()
    if not path or not os.path.exists(path):
        return None
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(path)
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        with open(tmp, "rb") as f:
            data = gzip.compress(f.read())
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    name = f"backup_{dt.date.today().isoformat()}.db.gz"
    return data, name


async def _make_dump() -> tuple[bytes, str] | None:
    if settings.is_sqlite:
        return await asyncio.to_thread(_dump_sqlite_sync)
    return await _dump_postgres()


@dataclass
class BackupResult:
    """Структурированный итог бэкапа. Раньше возвращалась только строка —
    вызывающий код не мог отличить успех от ошибки и помечал день
    выполненным даже когда бэкап НЕ ушёл (см. tasks._offsite_backup)."""
    ok: bool
    message: str

    def __str__(self) -> str:          # совместимость со старым выводом
        return self.message


async def send_backup_to_owner() -> BackupResult:
    """
    Делает дамп базы и отправляет владельцу площадки в Telegram файлом.
    Возвращает BackupResult: ok=False означает, что копии за сегодня нет и
    попытку нужно повторить (день не помечается выполненным).
    """
    owner_id = settings.platform_owner_tg_id
    if not owner_id:
        return BackupResult(False,
            "PLATFORM_OWNER_TG_ID не задан — некому отправлять бэкап. "
            "Задайте переменную в Railway (ваш Telegram ID).")

    result = await _make_dump()
    if result is None:
        return BackupResult(False,
            "Не удалось создать дамп базы (подробности в логах сервиса).")
    data, filename = result

    size_mb = len(data) / (1024 * 1024)
    if size_mb > MAX_TELEGRAM_FILE_MB:
        # повторять бессмысленно — размер сам не уменьшится; но и успехом
        # это не является: нужен внешний storage, оператора надо оповестить
        return BackupResult(False,
            f"Дамп получился {size_mb:.1f} МБ — это больше лимита "
            f"Telegram-бота на отправку файлов ({MAX_TELEGRAM_FILE_MB} МБ). "
            "Бэкап не отправлен: нужно внешнее хранилище (S3/Backblaze) "
            "вместо/вместе с отправкой в Telegram.")

    from app.bots import telegram as tg
    caption = f"💾 Бэкап базы за {dt.date.today().isoformat()} ({size_mb:.1f} МБ)"
    ok = await tg.send_document_to_owner(owner_id, filename, data, caption=caption)
    if not ok:
        return BackupResult(False,
            "Дамп создан, но отправить в Telegram не удалось (бот недоступен?).")
    return BackupResult(True, f"Бэкап отправлен: {filename} ({size_mb:.1f} МБ).")
