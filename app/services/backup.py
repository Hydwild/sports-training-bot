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
import io
import logging
import os

from app.core.config import settings

logger = logging.getLogger("backup")

# Telegram ограничивает файлы, отправляемые ботом, 50 МБ — оставляем запас
MAX_TELEGRAM_FILE_MB = 45

# Размер куска при потоковой обработке дампа.
#
# Зачем поток. Раньше дамп жил в памяти целиком и не в одном экземпляре:
# сырой вывод pg_dump, его gzip-копия, ещё одна полная копия при проверке
# (gzip.decompress) и зашифрованный результат. Пик RSS был порядка размера
# всей базы, а Python не всегда возвращает освобождённые блоки ОС — процесс
# так и оставался раздутым до перезапуска. Railway же считает деньги по
# СРЕДНЕЙ памяти, поэтому суточный бэкап оплачивался все 24 часа.
_DUMP_CHUNK = 1 << 20      # 1 МиБ


def _release_memory() -> None:
    """Возвращает освобождённую память операционной системе.

    После бэкапа освобождаются десятки/сотни мегабайт, но арены glibc
    остаются за процессом, и RSS не опускается. gc + malloc_trim отдают их
    обратно. На musl/Windows функции нет — тогда просто пропускаем."""
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _pg_dump_url() -> str:
    """DATABASE_URL для pg_dump: убираем SQLAlchemy-специфичный суффикс
    диалекта (+asyncpg) — pg_dump понимает только стандартный postgresql://."""
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _drain_stderr(stream, keep: int = 4096) -> bytes:
    """Вычитывает stderr до конца, оставляя в памяти только начало.

    Читать его обязательно и параллельно со stdout: если pg_dump напишет
    много предупреждений, он заблокируется на переполненном пайпе, а мы —
    на чтении stdout, и бэкап зависнет навсегда."""
    if stream is None:
        return b""
    head = b""
    while True:
        chunk = await stream.read(_DUMP_CHUNK)
        if not chunk:
            return head
        if len(head) < keep:
            head += chunk[:keep - len(head)]


async def _dump_postgres() -> tuple[bytes, str] | None:
    """pg_dump (обычный SQL, без владельцев/прав — переносимо на любой
    хостинг) -> gzip ПОТОКОМ. None при ошибке (см. логи).

    Сырой дамп целиком в память не поднимается: он сжимается по мере
    чтения, так что пик определяется размером архива, а не базы."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pg_dump", "--no-owner", "--no-acl", "--dbname", _pg_dump_url(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.error("pg_dump не найден в образе (нужен пакет postgresql-client)")
        return None

    err_task = asyncio.ensure_future(_drain_stderr(proc.stderr))
    buf = io.BytesIO()
    raw_len = 0
    try:
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            while True:
                chunk = await proc.stdout.read(_DUMP_CHUNK)
                if not chunk:
                    break
                raw_len += len(chunk)
                gz.write(chunk)
    finally:
        await proc.wait()
        stderr = await err_task

    if proc.returncode != 0:
        logger.error("pg_dump завершился с ошибкой (%s): %s",
                     proc.returncode, stderr.decode(errors="replace")[:500])
        return None
    if not raw_len:
        logger.error("pg_dump вернул пустой дамп")
        return None
    data = buf.getvalue()
    buf.close()
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
        # сжимаем потоком: файл базы целиком в память не поднимаем
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz, open(tmp, "rb") as f:
            while True:
                chunk = f.read(_DUMP_CHUNK)
                if not chunk:
                    break
                gz.write(chunk)
        data = buf.getvalue()
        buf.close()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    name = f"backup_{dt.date.today().isoformat()}.db.gz"
    return data, name


async def _make_dump() -> tuple[bytes, str] | None:
    if settings.is_sqlite:
        return await asyncio.to_thread(_dump_sqlite_sync)
    return await _dump_postgres()


# Метка в начале зашифрованного архива: по ней инструмент восстановления
# сразу отличает шифрованную копию от обычного .gz и не пытается её
# распаковать «как есть».
ENC_MAGIC = b"NEOBK1\n"


def encryption_enabled() -> bool:
    return bool((settings.backup_enc_key or "").strip())


def _backup_fernet():
    """Fernet на ключе BACKUP_ENC_KEY. Отдельный ключ: копия уходит в
    Telegram, и он не должен совпадать с тем, что лежит рядом с ней."""
    from cryptography.fernet import Fernet

    from app.core.phones import _fernet_key

    secret = (settings.backup_enc_key or "").strip()
    if not secret:
        raise RuntimeError("BACKUP_ENC_KEY не задан")
    return Fernet(_fernet_key(secret))


def encrypt_backup(data: bytes) -> bytes:
    """Шифрует архив целиком, с проверкой целостности (Fernet = AES-CBC +
    HMAC): подменённый файл не расшифруется, а не «расшифруется мусором»."""
    return ENC_MAGIC + _backup_fernet().encrypt(data)


def decrypt_backup(blob: bytes) -> bytes:
    """Расшифровывает копию. Бросает исключение при неверном ключе или
    подмене данных — молча возвращать мусор здесь нельзя."""
    from cryptography.fernet import InvalidToken

    if not blob.startswith(ENC_MAGIC):
        raise ValueError("Это не зашифрованная копия (нет метки NEOBK1)")
    try:
        return _backup_fernet().decrypt(blob[len(ENC_MAGIC):])
    except InvalidToken as e:
        raise ValueError("Неверный ключ или файл повреждён/подменён") from e


def checksum(data: bytes) -> str:
    """SHA-256 архива. Уходит в подпись сообщения, чтобы при восстановлении
    можно было убедиться, что скачан именно тот файл и он не побился."""
    import hashlib
    return hashlib.sha256(data).hexdigest()


def verify_dump(data: bytes) -> str:
    """Проверка, что архив действительно содержит базу, а не пустышку.
    Возвращает описание проблемы или пустую строку.

    Без неё «успешный» бэкап мог оказаться архивом из нуля таблиц: файл
    приходит, оператор спокоен, а восстанавливать нечего.

    Распаковываем ПОТОКОМ и держим лишь небольшое окно: полный
    gzip.decompress поднимал бы в RAM ещё одну копию всей базы рядом с
    архивом — на этом пике процесс и разрастался."""
    marker = b"CREATE TABLE"
    need_marker = not settings.is_sqlite
    head, tail = b"", b""
    total = 0
    found = False
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            while True:
                chunk = gz.read(_DUMP_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if len(head) < 64:
                    head += chunk[:64 - len(head)]
                if need_marker and not found and marker in tail + chunk:
                    found = True
                # всё, что нужно для вердикта, уже известно — дальше не
                # распаковываем: это чистая трата памяти и времени
                if total >= 1024 and (found or not need_marker):
                    break
                tail = chunk[-(len(marker) - 1):]
    except (OSError, EOFError) as e:
        return f"архив не распаковывается ({e})"
    if total < 1024:
        return f"дамп подозрительно мал ({total} байт)"
    if settings.is_sqlite:
        if not head.startswith(b"SQLite format 3"):
            return "это не файл базы SQLite"
    elif not found:
        return "в дампе нет ни одной таблицы"
    return ""


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

    Память после себя возвращаем ОС явно (см. _release_memory): иначе пик
    суточного бэкапа оставался бы в RSS до перезапуска и оплачивался все
    последующие часы.
    """
    try:
        return await _send_backup_to_owner()
    finally:
        _release_memory()


async def _send_backup_to_owner() -> BackupResult:
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

    digest = checksum(data)
    size_mb = len(data) / (1024 * 1024)
    if size_mb > MAX_TELEGRAM_FILE_MB:
        # повторять бессмысленно — размер сам не уменьшится; но и успехом
        # это не является: нужен внешний storage, оператора надо оповестить
        return BackupResult(False,
            f"Дамп получился {size_mb:.1f} МБ — это больше лимита "
            f"Telegram-бота на отправку файлов ({MAX_TELEGRAM_FILE_MB} МБ). "
            "Бэкап не отправлен: нужно внешнее хранилище (S3/Backblaze) "
            "вместо/вместе с отправкой в Telegram.")

    # содержимое проверяем последним: сначала отсекаем то, что вообще
    # нельзя отправить по размеру — там причина понятнее
    problem = verify_dump(data)
    if problem:
        return BackupResult(False,
            f"Дамп создан, но не прошёл проверку: {problem}. "
            "Отправлять такую копию нельзя — восстанавливать из неё нечего.")

    # Шифруем ПОСЛЕ проверки содержимого: проверять зашифрованное нечем,
    # а отправлять непроверенное нельзя.
    if encryption_enabled():
        try:
            data = encrypt_backup(data)
        except Exception as e:
            return BackupResult(False,
                f"Копия не зашифрована ({type(e).__name__}) — не отправляем. "
                "Проверьте BACKUP_ENC_KEY.")
        filename += ".enc"
        digest = checksum(data)
        size_mb = len(data) / (1024 * 1024)
    elif settings.is_pro:
        # В Pro копия уходит в Telegram — без шифрования это отправка всей
        # базы в чат. День не закрываем, чтобы попытка повторилась после
        # настройки ключа.
        return BackupResult(False,
            "BACKUP_ENC_KEY не задан: копия содержит персональные данные и "
            "без шифрования не отправляется. Задайте ключ и храните его "
            "отдельно от резервных копий.")

    from app.bots import telegram as tg
    caption = (f"💾 Бэкап базы за {dt.date.today().isoformat()} "
               f"({size_mb:.1f} МБ)\nSHA-256: {digest}")
    ok = await tg.send_document_to_owner(owner_id, filename, data, caption=caption)
    if not ok:
        return BackupResult(False,
            "Дамп создан, но отправить в Telegram не удалось (бот недоступен?).")
    return BackupResult(True, f"Бэкап отправлен: {filename} ({size_mb:.1f} МБ), "
                              f"SHA-256 {digest[:16]}…")
