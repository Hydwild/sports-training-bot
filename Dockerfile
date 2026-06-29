FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /code

# системные зависимости для asyncpg/matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Каталог для данных (SQLite-файл, логи) — монтируется как volume
RUN mkdir -p /data

EXPOSE 8000

# Прод: миграции применяются отдельно (alembic upgrade head в entrypoint/compose).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
