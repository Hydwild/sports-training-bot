# Переход клиентских Telegram/VK-ботов на webhook

## 1. Переменные Railway

Задайте до деплоя:

```env
PUBLIC_BASE_URL=https://sports-training-bot-production.up.railway.app
WEBHOOK_MASTER_SECRET=<случайная строка не короче 32 символов>
OUTBOX_IDLE_MIN_SECONDS=10
OUTBOX_IDLE_MAX_SECONDS=30
```

Секрет можно сгенерировать локально:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`WEBHOOK_MASTER_SECRET` нельзя менять как обычную настройку: от него и
токена бота выводится секрет, который уже записан у Telegram/VK. При его
ротации нужно повторно зарегистрировать webhook каждого клиента.

## 2. Деплой кода и схемы

`start.sh` запускает Alembic до приложения. После зелёного деплоя проверьте:

- `/health` отвечает `status=ok`;
- в логах нет ошибок миграции `2f6a8c91d4e7`;
- RSS не растёт скачком примерно на 570 МБ: `vkbottle` больше не входит в
  runtime-зависимости;
- существующие polling/Long Poll боты продолжают отвечать — миграция не
  переключает их автоматически.

## 3. Canary одного клиента

Откройте `/admin/platform/{tenant_id}/edit`.

1. Нажмите «Перевести на webhook» у Telegram.
2. Отправьте боту `/start`, откройте список записей и нажмите callback-кнопку.
3. Нажмите «Перевести на Callback API» у VK.
4. Отправьте сообществу текст и нажмите inline-кнопку.
5. Убедитесь, что запись, сделанная в одном канале, видна в другом и на сайте.

Панель сама вызывает API мессенджера. Режим меняется только после успешной
регистрации. Telegram pending updates не удаляются. VK confirmation code
получается через API и хранится отдельно от секрета событий.

То же через служебный API:

```bash
curl -X PUT "$PUBLIC_BASE_URL/api/tenants/ID/delivery/tg" \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"webhook"}'

curl -X PUT "$PUBLIC_BASE_URL/api/tenants/ID/delivery/vk" \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"callback"}'
```

## 4. Остальные клиенты

Переключайте небольшими партиями и после каждой проверяйте сообщения и
кнопки. Новые клиенты, созданные через панель, уже предлагают webhook и
Callback API по умолчанию. Старые строки сохраняют `polling/longpoll`, пока
оператор не нажмёт кнопку.

## 5. Откат

Через ту же страницу нажмите «Вернуть polling» или «Вернуть Long Poll».
Через API:

```bash
curl -X PUT "$PUBLIC_BASE_URL/api/tenants/ID/delivery/tg" \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"polling"}'

curl -X PUT "$PUBLIC_BASE_URL/api/tenants/ID/delivery/vk" \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"longpoll"}'
```

Для VK Long Poll должен быть задан `RUN_VK_POLLING=true`. Не заменяйте и не
удаляйте токен, пока бот в webhook/callback: сначала выполните откат режима,
потом меняйте токен и при необходимости включайте webhook заново.

## Что изменилось в нагрузке

- клиентские TG polling и VK Long Poll не держат постоянные запросы после
  перехода;
- webhook быстро коммитится в `inbound_events`, HTTP получает подтверждение,
  а обработка идёт отдельным worker'ом с дедупликацией и retry;
- пустой outbox проверяется через 10, 20 и затем 30 секунд, но новый commit
  будит доставщик сразу;
- входящий payload очищается после успешной обработки или dead-letter и
  служебная строка удаляется через 24 часа.
