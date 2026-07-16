"""Промо-страница продукта: GET /promo. Отредактируйте контакты и цены."""

PROMO_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Бот записи на тренировки — Telegram + ВКонтакте</title><style>
:root{--c:#3a7bd5}*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0;color:#223;background:#fff}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px}
.hero{background:linear-gradient(135deg,var(--c),#00d2ff);color:#fff;
text-align:center;padding:56px 16px}
.hero h1{font-size:30px;margin:0 0 12px}.hero p{font-size:18px;opacity:.95}
.btn{display:inline-block;background:#fff;color:var(--c);font-weight:700;
padding:14px 26px;border-radius:12px;text-decoration:none;margin:8px}
.btn.o{background:transparent;color:#fff;border:2px solid #fff}
h2{text-align:center;margin:40px 0 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
gap:14px;margin-top:18px}
.card{background:#f6f8fc;border-radius:14px;padding:18px}
.card b{display:block;margin-bottom:6px}
.steps{counter-reset:s}.step{display:flex;gap:14px;margin:14px 0;align-items:flex-start}
.step::before{counter-increment:s;content:counter(s);background:var(--c);
color:#fff;border-radius:50%;min-width:32px;height:32px;display:flex;
align-items:center;justify-content:center;font-weight:700}
.trust{background:#f6f8fc;border-radius:14px;padding:18px;margin-top:26px}
.price{border:2px solid var(--c);border-radius:14px;padding:22px;text-align:center}
footer{text-align:center;color:#889;padding:26px}
</style></head><body>
<div class="hero"><h1>🏸 Бот записи на тренировки</h1>
<p>Telegram + ВКонтакте + веб-страница. Участники записываются сами,<br>
расписание создаёт тренировки автоматически, тренер видит явку и оплату.</p>
<a class="btn" href="#demo">Попробовать демо</a>
<a class="btn o" href="#price">Цена</a></div>
<div class="wrap">
<h2>Что умеет</h2>
<div class="grid">
<div class="card"><b>📲 Запись в один клик</b>Из Telegram, ВК или браузера —
участнику не нужно ничего устанавливать. Очередь при заполнении.</div>
<div class="card"><b>📆 Автопилот расписания</b>Задайте «вторник и четверг
19:00» — тренировки создаются сами, подписчики получают уведомление.</div>
<div class="card"><b>⏰ Напоминания</b>«Скоро тренировка» за выбранное время —
меньше неявок.</div>
<div class="card"><b>✅ Явка и оплата</b>Отметки в одно касание, должники,
экспорт списков в CSV/Excel/PDF.</div>
<div class="card"><b>📊 Статистика и рейтинг</b>Топ посещаемости, сводка по
месяцам, график — мотивирует участников.</div>
<div class="card"><b>🌐 Страница записи + QR</b>Ссылка и QR-код для зала —
записываются даже те, у кого нет мессенджеров.</div>
</div>
<h2>Как это работает</h2>
<div class="steps">
<div class="step"><div><b>Разворачиваем бота под ваш клуб</b><br>
Ваше название, ваши боты, ваши цвета. Готово за один день.</div></div>
<div class="step"><div><b>Даёте участникам ссылку</b><br>
Они записываются сами — из Telegram, ВК или по QR в зале.</div></div>
<div class="step"><div><b>Тренируете, а не администрируете</b><br>
Расписание, напоминания и списки бот ведёт сам.</div></div>
</div>
<div class="trust"><b>Надёжность и данные</b><br>
Ежедневные резервные копии базы и кнопка «скачать бэкап» · данные клуба
изолированы · уведомление владельцу при сбоях · 50 автоматических тестов
при каждом обновлении.</div>
<h2 id="price">Цена</h2>
<div class="price"><b>Свой бот под ключ</b><br><br>
Настройка под клуб + запуск + инструкция.<br>
<!-- УКАЖИТЕ ВАШУ ЦЕНУ И УСЛОВИЯ -->
Стоимость и условия — по запросу.<br><br>
<a class="btn" style="background:var(--c);color:#fff"
href="https://t.me/ВАШ_КОНТАКТ">Написать в Telegram</a></div>
<h2 id="demo">Живое демо</h2>
<p style="text-align:center">Посмотрите страницу записи демо-клуба:
<a href="/club/1">открыть демо</a> — можно записаться и отменить запись.</p>
</div>
<footer>Бот записи на тренировки · Telegram + VK + Web</footer>
</body></html>"""
