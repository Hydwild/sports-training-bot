FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # glibc по умолчанию заводит до 8*CPU арен malloc и почти не отдаёт
    # освобождённую память ОС. Для однопроцессного asyncio-приложения это
    # чистые потери RSS, а Railway тарифицирует именно среднюю память.
    MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=131072

# SHA собираемого коммита — отдаётся в /health, чтобы видеть, какой код в
# проде. Передаётся при сборке: docker build --build-arg GIT_SHA=$(git rev-parse HEAD)
ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA}

WORKDIR /code

# системные зависимости: gcc/libpq-dev — сборка asyncpg/matplotlib;
# postgresql-client-18 — pg_dump для внешних бэкапов (app/services/backup.py).
# pg_dump отказывается снимать дамп с сервера НОВЕЕ самого себя — Railway
# Managed Postgres сейчас на 18.x, а в стандартном репозитории Debian есть
# только 17.x, поэтому клиент берём из официального репозитория PostgreSQL
# (apt.postgresql.org). Если Railway обновит Postgres до 19.x, здесь тоже
# нужно будет поднять версию пакета.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev curl ca-certificates gnupg lsb-release \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Каталог для данных (SQLite-файл, логи)
RUN mkdir -p /data && chmod +x start.sh

EXPOSE 8000

# Старт через скрипт: слушает $PORT (Railway) или 8000 (локально),
# для Pro применяет миграции.
CMD ["sh", "start.sh"]
