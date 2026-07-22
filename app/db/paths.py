"""Безопасное извлечение файловых путей из URL базы данных."""
from sqlalchemy.engine import make_url


def sqlite_file_path(database_url: str) -> str | None:
    """Вернуть путь SQLite как его понимает SQLAlchemy.

    Ручной подсчёт слешей путал относительный URL
    ``sqlite+aiosqlite:///./badminton.db`` с абсолютным ``/badminton.db``.
    ``make_url`` одинаково корректно разбирает относительные, POSIX- и
    Windows-пути.
    """
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return None
    return url.database

