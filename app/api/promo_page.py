"""Промо-страница продукта: GET /promo. Отредактируйте текст ниже при необходимости."""
from app.api.public_style import SITE_CSS, TELEGRAM_CONTACT, site_footer, site_nav

_EXTRA_CSS = """
.steps{counter-reset:s;display:flex;flex-direction:column;gap:0;margin-top:18px}
.step{display:flex;gap:18px;align-items:flex-start;padding:18px 0;
  border-bottom:1px solid var(--border)}
.step:last-child{border-bottom:0}
.step::before{counter-increment:s;content:counter(s);flex-shrink:0;
  font:400 20px/40px Georgia,serif;color:var(--gold);width:40px;height:40px;
  border-radius:50%;border:1px solid var(--gold);text-align:center}
.step b{display:block;font:700 15px/1.4 -apple-system,system-ui,sans-serif;margin-bottom:4px}
.step p{margin:0;font:400 14px/1.55 -apple-system,system-ui,sans-serif;color:var(--muted)}
.card b.title{display:block;font:700 15px/1.3 -apple-system,system-ui,sans-serif;
  margin-bottom:6px}
.card p{margin:0;font:400 14px/1.55 -apple-system,system-ui,sans-serif;color:var(--muted)}
.trust{display:flex;gap:24px;flex-wrap:wrap;padding:24px;border-radius:16px;
  background:var(--surface-2);border:1px solid var(--border);margin-top:18px}
.trust .item{flex:1;min-width:180px;font:400 14px/1.55 -apple-system,system-ui,sans-serif;
  color:var(--muted)}
.trust .item b{display:block;color:var(--ink);font:700 13.5px/1.3
  -apple-system,system-ui,sans-serif;margin-bottom:3px}
.price-card{border:1px solid var(--gold);border-radius:20px;padding:34px 30px;
  text-align:center;background:var(--surface);box-shadow:var(--shadow)}
.price-card .tag{display:inline-block;font:700 11px/1 -apple-system,system-ui,sans-serif;
  letter-spacing:.1em;text-transform:uppercase;color:var(--gold);margin-bottom:10px}
.price-card h3{font:400 24px/1.3 Georgia,serif;margin:0 0 10px}
.price-card p{color:var(--muted);font:400 14.5px/1.6 -apple-system,system-ui,sans-serif;
  max-width:44ch;margin:0 auto 22px}
.demo{text-align:center;padding:8px 0 10px;font:400 14.5px/1.6 -apple-system,
  system-ui,sans-serif;color:var(--muted)}
"""

PROMO_HTML = ("""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Бот записи на тренировки — Telegram + ВКонтакте</title><style>
""" + SITE_CSS + _EXTRA_CSS + """
</style></head><body>
""" + site_nav("promo") + """
<div class="wrap">

<div class="hero">
  <span class="eyebrow">Telegram · ВКонтакте · веб-страница записи</span>
  <h1>Пока вы тренируете —<br>запись ведёт себя сама</h1>
  <p>Участники записываются сами, встают в очередь и получают напоминания.
    Расписание создаёт тренировки автоматически, вы просто отмечаете явку.</p>
  <div class="cta-row">
    <a class="btn-gold" href="#demo">Живое демо</a>
    <a class="btn-ghost" href="#price">Как подключить</a>
  </div>
</div>

<h2 class="section">Что умеет</h2>
<p class="section-lead">Всё, что обычно решают табличкой, перепиской и терпением.</p>
<div class="grid">
<div class="card"><b class="title">📲 Запись в один клик</b><p>Из Telegram, ВК
  или браузера — участнику не нужно ничего устанавливать. Очередь при заполнении
  мест, автоматический подъём при отмене.</p></div>
<div class="card"><b class="title">📆 Автопилот расписания</b><p>Задайте
  «вторник и четверг 19:00» — тренировки создаются сами, подписчики получают
  уведомление об открытии записи.</p></div>
<div class="card"><b class="title">⏰ Напоминания</b><p>«Скоро тренировка» за
  выбранное время до начала — меньше неявок и путаницы.</p></div>
<div class="card"><b class="title">✅ Явка и оплата</b><p>Отметки в одно
  касание, список должников, экспорт в CSV / Excel / PDF для отчётов.</p></div>
<div class="card"><b class="title">📊 Статистика и рейтинг</b><p>Топ
  посещаемости клуба, личный профиль участника — мотивирует не пропускать.</p></div>
<div class="card"><b class="title">🌐 Страница записи + QR</b><p>Ссылка и
  QR-код для зала — записываются даже те, у кого нет мессенджеров.</p></div>
</div>

<h2 class="section">Как это работает</h2>
<div class="card" style="max-width:640px;margin:0 auto">
<div class="steps">
<div class="step"><div><b>Разворачиваем бота под ваш клуб</b>
  <p>Ваше название, ваши боты, ваши цвета — готово за один день.</p></div></div>
<div class="step"><div><b>Даёте участникам ссылку</b>
  <p>Они записываются сами — из Telegram, ВК или по QR прямо в зале.</p></div></div>
<div class="step"><div><b>Тренируете, а не администрируете</b>
  <p>Расписание, напоминания и списки участников бот ведёт сам.</p></div></div>
</div>
</div>

<h2 class="section">Надёжность и данные</h2>
<div class="trust">
  <div class="item"><b>Ежедневные бэкапы</b>резервная копия базы хранится вне
    сервера, переживает падение платформы</div>
  <div class="item"><b>Изоляция клубов</b>данные каждого клуба технически
    недоступны другим</div>
  <div class="item"><b>Алерты о сбоях</b>владелец сразу узнаёт, если что-то
    пошло не так</div>
  <div class="item"><b>Автотесты при каждом обновлении</b>изменения не
    выкатываются без проверки</div>
</div>

<h2 class="section" id="price">Тариф под задачу</h2>
<p class="section-lead">От одного тренера до сети клубов — редакция включается флагом,
  код общий.</p>
<div class="price-card">
  <span class="tag">Свой бот под ключ</span>
  <h3>Настройка под клуб + запуск + инструкция</h3>
  <p>Стоимость и условия — по запросу, зависят от редакции (Lite/Pro) и числа групп.</p>
  <a class="btn-gold" href=\"""" + TELEGRAM_CONTACT + """\">Написать в Telegram</a>
</div>

<h2 class="section" id="demo">Живое демо</h2>
<p class="demo">Посмотрите страницу записи демо-клуба:
  <a href="/club/1">открыть демо</a> — можно записаться и отменить запись.</p>

""" + site_footer() + """
</div>
</body></html>""")
