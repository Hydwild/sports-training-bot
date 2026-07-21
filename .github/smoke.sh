#!/usr/bin/env bash
# Smoke-проверка запущенного контейнера.
#   $1 — порт, $2 — метка (lite/pro), $3 — ожидаемый db (sqlite/postgres)
set -e
PORT="$1"; LABEL="$2"; WANT_DB="$3"
BASE="http://localhost:$PORT"

for _ in $(seq 1 40); do
  curl -fsS "$BASE/health" > /dev/null 2>&1 && break
  sleep 2
done

health=$(curl -fsS "$BASE/health") || {
  echo "::error title=Smoke $LABEL::/health не ответил"; exit 1; }
echo "$LABEL health: $health"

echo "$health" | grep -q '"status":"ok"' || {
  echo "::error title=Smoke $LABEL::status не ok"; exit 1; }
echo "$health" | grep -q "\"db\":\"$WANT_DB\"" || {
  echo "::error title=Smoke $LABEL::db не $WANT_DB ($health)"; exit 1; }

if [ "$LABEL" = "pro" ]; then
  echo "$health" | grep -q '"edition":"pro"' || {
    echo "::error title=Smoke pro::edition не pro"; exit 1; }
fi

for path in /promo /faq /privacy /reviews; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$path")
  echo "$LABEL $path -> $code"
  [ "$code" = "200" ] || {
    echo "::error title=Smoke $LABEL::$path вернул $code"; exit 1; }
done
