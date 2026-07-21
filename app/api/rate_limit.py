"""
Ограничение частоты запросов к публичным формам.

Счётчик в памяти процесса лимитом является только пока процесс один: два
воркера дают двойной лимит, а перезапуск обнуляет счёт. Поэтому в Pro
(PostgreSQL) счётчик общий — строка в таблице rate_buckets, инкремент
атомарный (INSERT ... ON CONFLICT DO UPDATE ... RETURNING).

SQLite (редакция Lite и локальная отладка) остаётся на счётчике в памяти:
там процесс заведомо один, а плодить блокировки файла ради этого незачем.
Режим выбирается явно и виден в логах при старте.
"""
from __future__ import annotations

import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

# окно, за которое считаем запросы, и предел в нём по умолчанию
DEFAULT_LIMIT = 5
DEFAULT_WINDOW = 60

# счётчик для Lite/dev: {ключ: [метки времени]}
_memory: dict[str, list[float]] = {}


def bucket_key(scope: str, tenant_id: int | None, client: str) -> str:
    """Ключ ограничения. Включает форму и клуб: всплеск записей в один
    клуб не должен закрывать вход в панель оператора или запись в
    соседний клуб."""
    return f"{scope}|{tenant_id or '-'}|{client}"[:200]


def shared_storage() -> bool:
    """Общий счётчик доступен только там, где есть PostgreSQL."""
    return not settings.is_sqlite


def _window_start(now: float, window: int) -> int:
    return int(now // window) * window


def check_memory(key: str, limit: int, window: int) -> tuple[bool, int]:
    """(разрешено, через сколько секунд пробовать снова)."""
    now = time.time()
    hits = [t for t in _memory.get(key, []) if now - t < window]
    if len(hits) >= limit:
        _memory[key] = hits
        retry = int(window - (now - min(hits))) + 1
        return False, max(retry, 1)
    hits.append(now)
    _memory[key] = hits
    # чистим только протухшие ключи: поток запросов с чужих адресов не
    # должен сбрасывать лимит всем сразу
    if len(_memory) > 5000:
        stale = [k for k, v in _memory.items()
                 if not any(now - t < window for t in v)]
        for k in stale:
            del _memory[k]
    return True, 0


async def check_shared(session: AsyncSession, key: str, limit: int,
                       window: int) -> tuple[bool, int]:
    """Атомарный инкремент общего счётчика в PostgreSQL."""
    from sqlalchemy.dialects.postgresql import insert

    from app.models.entities import RateBucket

    now = time.time()
    start = _window_start(now, window)
    stmt = (insert(RateBucket)
            .values(bucket_key=key, window_start=start, hits=1)
            .on_conflict_do_update(
                index_elements=[RateBucket.bucket_key, RateBucket.window_start],
                set_={"hits": RateBucket.hits + 1})
            .returning(RateBucket.hits))
    hits = (await session.execute(stmt)).scalar_one()
    await session.commit()
    if hits > limit:
        retry = int(start + window - now) + 1
        return False, max(retry, 1)
    return True, 0


async def allow(session: AsyncSession | None, *, scope: str,
                tenant_id: int | None, client: str,
                limit: int = DEFAULT_LIMIT,
                window: int = DEFAULT_WINDOW) -> tuple[bool, int]:
    """(разрешено, Retry-After в секундах)."""
    key = bucket_key(scope, tenant_id, client)
    if session is not None and shared_storage():
        return await check_shared(session, key, limit, window)
    return check_memory(key, limit, window)


async def purge_old_buckets(session: AsyncSession,
                            older_than: int = 3600) -> int:
    """Убирает отработавшие окна: без чистки таблица растёт вечно."""
    from sqlalchemy import delete

    from app.models.entities import RateBucket

    cutoff = int(time.time()) - older_than
    res = await session.execute(
        delete(RateBucket).where(RateBucket.window_start < cutoff))
    return res.rowcount or 0
