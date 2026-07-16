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

# Если в образе есть seed.db (собран конфигуратором /admin/builder) и на
# постоянном диске ещё нет базы — копируем: клуб и тренер уже настроены,
# ничего вручную через Swagger заполнять не нужно. Не трогает существующие
# данные (копирует только при первом запуске, когда файла в /data ещё нет).
if [ -f /code/seed.db ] && [ ! -f /data/badminton.db ]; then
  mkdir -p /data
  cp /code/seed.db /data/badminton.db
  echo "Стартовые данные клуба скопированы в /data/badminton.db"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
