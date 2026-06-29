#!/usr/bin/env bash
#
# Развёртывание бота одной командой.
#
#   ./deploy.sh lite    — версия для одного тренера (SQLite)
#   ./deploy.sh pro     — версия для клуба (PostgreSQL)
#
# Скрипт:
#   1) проверяет наличие Docker и docker compose (ставит Docker, если нужно и можно),
#   2) готовит файл .env из шаблона нужной редакции (если .env ещё нет),
#   3) собирает и запускает контейнеры,
#   4) показывает статус и адрес.
#
set -euo pipefail

EDITION="${1:-}"
if [[ "$EDITION" != "lite" && "$EDITION" != "pro" ]]; then
  echo "Использование: ./deploy.sh lite | pro"
  echo "  lite — для одного тренера (запись, очередь, напоминания, посещаемость)"
  echo "  pro  — для клуба (доп: статистика, оплаты, несколько групп)"
  exit 1
fi

COMPOSE_FILE="docker-compose.${EDITION}.yml"
ENV_EXAMPLE=".env.${EDITION}.example"

echo "==> Развёртывание редакции: ${EDITION}"

# 1) Docker
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не найден. Пытаюсь установить (нужен root/sudo)..."
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
  else
    echo "Нет curl. Установите Docker вручную: https://docs.docker.com/engine/install/"
    exit 1
  fi
fi

# docker compose (v2) или docker-compose (v1)
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "Не найден docker compose. Установите плагин compose."
  exit 1
fi

# 2) .env
if [[ ! -f .env ]]; then
  cp "$ENV_EXAMPLE" .env
  echo "==> Создан .env из ${ENV_EXAMPLE}"
  echo "    ВАЖНO: впишите TG_TOKEN (и для pro — JWT_SECRET, ADMIN_API_TOKEN)."
  if [[ "$EDITION" == "pro" ]]; then
    # автогенерация секретов для удобства
    if command -v openssl >/dev/null 2>&1; then
      JWT=$(openssl rand -hex 32)
      ADM=$(openssl rand -hex 16)
      sed -i.bak "s|^JWT_SECRET=.*|JWT_SECRET=${JWT}|" .env
      sed -i.bak "s|^ADMIN_API_TOKEN=.*|ADMIN_API_TOKEN=${ADM}|" .env
      rm -f .env.bak
      echo "==> Сгенерированы JWT_SECRET и ADMIN_API_TOKEN."
    fi
  fi
  echo ""
  read -r -p "Откройте .env, впишите TG_TOKEN и нажмите Enter для продолжения..."
fi

# 3) сборка и запуск
echo "==> Сборка и запуск контейнеров..."
$DC -f "$COMPOSE_FILE" up -d --build

# 4) статус
echo ""
echo "==> Готово. Статус:"
$DC -f "$COMPOSE_FILE" ps
echo ""
echo "Приложение: http://localhost:8000  (health: /health)"
if [[ "$EDITION" == "pro" ]]; then
  echo "Админка:    http://localhost:8000/admin"
fi
echo ""
echo "Логи:    $DC -f $COMPOSE_FILE logs -f app"
echo "Стоп:    $DC -f $COMPOSE_FILE down"
