#!/usr/bin/env sh
# Стартовый скрипт для контейнера.
# Railway (и некоторые другие платформы) передают порт в переменной $PORT.
# Локально/в docker-compose её нет — тогда используем 8000.
#
# Для Pro (PostgreSQL) применяем миграции Alembic перед стартом.
# Для Lite (SQLite) таблицы создаются автоматически при старте приложения,
# поэтому миграции не обязательны — но если БД доступна, пытаемся применить.

PORT="${PORT:-8000}"

if [ "$EDITION" = "pro" ]; then
  echo "PRO: applying migrations..."
  alembic upgrade head || echo "WARN: alembic failed (продолжаем старт)"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
