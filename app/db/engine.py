"""
Async-движок SQLAlchemy. Работает и со SQLite (aiosqlite), и с PostgreSQL
(asyncpg) — выбор определяется строкой DATABASE_URL.
"""
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# echo=False — не засорять логи SQL; включите True при отладке запросов.
from sqlalchemy.pool import NullPool

# Для SQLite отключаем пул: aiosqlite-соединения привязаны к event loop,
# и переиспользование их между циклами (тесты, фоновые задачи) приводит
# к зависаниям. Открытие соединения в SQLite дёшево.
_kw = {"poolclass": NullPool} if settings.database_url.startswith("sqlite") else {}
engine = create_async_engine(settings.database_url, echo=False, future=True, **_kw)


# Для SQLite включаем поддержку внешних ключей (по умолчанию выключена).
if settings.is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_fk_pragma(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: выдаёт сессию и гарантированно закрывает её."""
    async with SessionLocal() as session:
        yield session
