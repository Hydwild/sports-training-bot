{% extends "base.html" %}
{% block content %}
<div class="card">
  <h2>Вход в админку</h2>
  <p class="muted">Войдите через Telegram. Доступ получают только владельцы,
     тренеры и ассистенты зарегистрированных клубов.</p>
  {% if widget %}
    <!-- Telegram Login Widget. data-auth-url ведёт на /admin/auth/telegram -->
    <script async src="https://telegram.org/js/telegram-widget.js?22"
      data-telegram-login="{{ bot_username }}"
      data-size="large"
      data-auth-url="{{ auth_url }}"
      data-request-access="write"></script>
  {% else %}
    <p class="muted">Telegram-виджет не настроен (нет TG_BOT_USERNAME).
       Для локальной отладки используйте dev-вход ниже.</p>
  {% endif %}
  {% if dev_login %}
    <form method="post" action="/admin/auth/dev" style="margin-top:16px">
      <input type="number" name="tg_user_id" placeholder="Telegram user id" required
             style="padding:8px;border:1px solid #ccd;border-radius:8px">
      <button class="btn" type="submit">Dev-вход</button>
    </form>
    <p class="muted">Dev-вход доступен только при ADMIN_DEV_LOGIN=true.</p>
  {% endif %}
</div>
{% endblock %}
