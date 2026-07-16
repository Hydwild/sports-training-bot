"""Общие фикстуры: чистая in-memory async БД на каждый тест."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/v2_testclient.db")
os.environ.setdefault("ADMIN_API_TOKEN", "tok")
os.environ.setdefault("TG_MODE", "webhook")  # без polling в тестах
os.environ.setdefault("RUN_VK_POLLING", "false")
os.environ.setdefault("DISABLE_BACKGROUND", "1")  # фоновые циклы не нужны в тестах
os.environ.setdefault("ADMIN_DEV_LOGIN", "true")
import os as _os
if _os.path.exists("/tmp/v2_testclient.db"): _os.remove("/tmp/v2_testclient.db")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("LOG_DIR", "/tmp/test_logs_v2")
os.environ.setdefault("TG_TOKEN", "123456:TESTTOKEN")

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.models.entities import Base


@pytest_asyncio.fixture
async def session():
    # отдельный in-memory engine на каждый тест (StaticPool держит одно соединение)
    from sqlalchemy.pool import StaticPool
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()
