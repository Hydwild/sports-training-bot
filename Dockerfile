FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /code

# системные зависимости: gcc/libpq-dev — сборка asyncpg/matplotlib;
# postgresql-client — pg_dump для внешних бэкапов (app/services/backup.py)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev postgresql-client \
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
