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
from sqlalchemy.orm import Session

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


@event.listens_for(Session, "after_flush")
def _remember_outbox_insert(session, _flush_context) -> None:  # noqa: ANN001
    # enqueue() часто делает flush до commit, поэтому метку ставим здесь,
    # пока новые ORM-объекты ещё доступны. Импорт модели не нужен и циклов нет.
    if any(getattr(type(obj), "__tablename__", "") == "outbox"
           for obj in session.new):
        session.info["outbox_inserted"] = True


@event.listens_for(Session, "after_commit")
def _wake_outbox_after_commit(session) -> None:  # noqa: ANN001
    if not session.info.pop("outbox_inserted", False):
        return
    from app.services.tasks import notify_outbox_committed
    notify_outbox_committed()


@event.listens_for(Session, "after_rollback")
def _forget_rolled_back_outbox(session) -> None:  # noqa: ANN001
    session.info.pop("outbox_inserted", None)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: выдаёт сессию и гарантированно закрывает её."""
    async with SessionLocal() as session:
        yield session
