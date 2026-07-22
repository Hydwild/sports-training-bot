"""Пути SQLite должны совпадать с теми, куда пишет SQLAlchemy."""
import pytest

from app.db.paths import sqlite_file_path


@pytest.mark.parametrize(("url", "expected"), [
    ("sqlite+aiosqlite:///./badminton.db", "./badminton.db"),
    ("sqlite+aiosqlite:////data/badminton.db", "/data/badminton.db"),
    ("sqlite+aiosqlite:///C:/data/badminton.db", "C:/data/badminton.db"),
    ("postgresql+asyncpg://user:pass@db/app", None),
])
def test_sqlite_file_path(url, expected):
    assert sqlite_file_path(url) == expected

