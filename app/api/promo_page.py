"""Промо-страница продукта: GET /promo. Отредактируйте текст ниже при необходимости."""
from app.api.public_style import (
    SITE_CSS, TELEGRAM_CONTACT, head_meta, site_footer, site_nav,
)

# Демо-бот в Telegram: демо-клуб (Tenant.is_demo=True), любой посетитель
# сам выбирает роль "тренер" или "участник" (см. app/bots/telegram.py,
# app/services/tasks.py::_demo_reset_daily — данные сбрасываются каждую ночь).
DEMO_BOT_URL = "https://t.me/Lecor3232_bot"

_EXTRA_CSS = """
.steps{counter-reset:s;display:flex;flex-direction:column;gap:0}
.step{display:flex;gap:16px;align-items:flex-start;padding:16px 0;
  border-bottom:1px solid var(--border)}
.step:first-child{padding-top:8px}
.step:last-child{border-bottom:0;padding-bottom:8px}
.step::before{counter-increment:s;content:counter(s);flex-shrink:0;
  font:400 20px/40px Georgia,serif;color:var(--gold);width:40px;height:40px;
  border-radius:50%;border:1px solid var(--gold);text-align:center}
.step b{display:block;font:600 15px/1.4 -apple-system,system-ui,sans-serif;margin-bottom:4px}
.step p{margin:0;font:400 14px/1.6 -apple-system,system-ui,sans-serif;color:var(--muted)}
.feature-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px}
.feature-card{background:var(--surface);border:1px solid var(--border);border-radius:16px;
  padding:24px;box-shadow:var(--shadow)}
.feature-icon{width:40px;height:40px;border-radius:12px;border:1px solid var(--gold);
  display:flex;align-items:center;justify-content:center;margin-bottom:16px}
.feature-icon svg{width:19px;height:19px;stroke:var(--gold);fill:none;stroke-width:1.6;
  stroke-linecap:round;stroke-linejoin:round}
.feature-card h3{font:400 17.5px/1.35 Georgia,serif;margin:0 0 8px;text-wrap:balance}
.feature-card p{margin:0;font:400 14px/1.6 -apple-system,system-ui,sans-serif;color:var(--muted)}
.trust{display:flex;gap:24px;flex-wrap:wrap;padding:24px;border-radius:16px;
  background:var(--surface-2);border:1px solid var(--border)}
.trust .item{flex:1;min-width:180px;font:400 14px/1.6 -apple-system,system-ui,sans-serif;
  color:var(--muted)}
.trust .item b{display:block;color:var(--ink);font:600 13.5px/1.35
  -apple-system,system-ui,sans-serif;margin-bottom:4px}
.price-card{border:1px solid var(--gold);border-radius:20px;padding:32px;
  text-align:center;background:var(--surface);box-shadow:var(--shadow)}
.price-card .tag{display:inline-block;font:700 11px/1 -apple-system,system-ui,sans-serif;
  letter-spacing:.1em;text-transform:uppercase;color:var(--gold);margin-bottom:12px}
.price-card h3{font:400 24px/1.3 Georgia,serif;margin:0 0 8px;text-wrap:balance}
.price-card p{color:var(--muted);font:400 14.5px/1.6 -apple-system,system-ui,sans-serif;
  max-width:44ch;margin:0 auto 24px}
.demo-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.demo-card{border-radius:20px;padding:24px;text-align:center;
  border:1px solid var(--border);background:var(--surface);box-shadow:var(--shadow);
  display:flex;flex-direction:column;align-items:center}
.demo-card.primary{border-color:var(--gold)}
.demo-card .tag{display:inline-block;font:700 11px/1 -apple-system,system-ui,sans-serif;
  letter-spacing:.1em;text-transform:uppercase;color:var(--gold);margin-bottom:12px}
.demo-card h3{font:400 19px/1.3 Georgia,serif;margin:0 0 8px;text-wrap:balance}
.demo-card p{color:var(--muted);font:400 13.5px/1.6 -apple-system,system-ui,sans-serif;
  margin:0 0 20px;flex:1}
.demo-card .btn-ghost{color:var(--ink);border-color:var(--border)}
.demo-card .btn-ghost:hover{border-color:var(--gold)}
@media (max-width:640px){.demo-grid{grid-template-columns:1fr}}
"""

PROMO_HTML = ("""<!doctype html><html lang="ru"><head>""" + head_meta(
    "Бот записи на тренировки — Telegram и ВКонтакте",
    "Запись на спортивные тренировки в Telegram, ВКонтакте и браузере: "
    "очередь, напоминания, явка и оплата, статистика. Есть живое демо.",
) + """<style>
""" + SITE_CSS + _EXTRA_CSS + """
</style></head><body>
""" + site_nav("promo") + """
<main class="wrap">

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
<div class="feature-grid">
<div class="feature-card">
  <div class="feature-icon"><svg viewBox="0 0 24 24"><path d="M5 13l4 4L19 7"/></svg></div>
  <h3>Запись в один клик</h3>
  <p>Из Telegram, ВК или браузера — участнику не нужно ничего устанавливать.
    Очередь при заполнении мест, автоматический подъём при отмене.</p>
</div>
<div class="feature-card">
  <div class="feature-icon"><svg viewBox="0 0 24 24">
    <rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/>
  </svg></div>
  <h3>Автопилот расписания</h3>
  <p>Задайте «вторник и четверг 19:00» — тренировки создаются сами,
    подписчики получают уведомление об открытии записи.</p>
</div>
<div class="feature-card">
  <div class="feature-icon"><svg viewBox="0 0 24 24">
    <path d="M6.5 9a5.5 5.5 0 0111 0c0 5.5 2 6.5 2 6.5h-15s2-1 2-6.5z"/><path d="M10.3 19.5a1.9 1.9 0 003.4 0"/>
  </svg></div>
  <h3>Напоминания</h3>
  <p>«Скоро тренировка» за выбранное время до начала — меньше неявок и путаницы.</p>
</div>
<div class="feature-card">
  <div class="feature-icon"><svg viewBox="0 0 24 24">
    <path d="M4 7h8M4 12h8M4 17h5"/><path d="M15.5 6.5l2 2 3.5-3.5"/><path d="M15.5 15.5l2 2 3.5-3.5"/>
  </svg></div>
  <h3>Явка и оплата</h3>
  <p>Отметки в одно касание, список должников, экспорт в CSV / Excel / PDF
    для отчётов.</p>
</div>
<div class="feature-card">
  <div class="feature-icon"><svg viewBox="0 0 24 24">
    <path d="M4.5 20V11M12 20V4M19.5 20v-6.5"/>
  </svg></div>
  <h3>Статистика и рейтинг</h3>
  <p>Топ посещаемости клуба, личный профиль участника — мотивирует не
    пропускать.</p>
</div>
<div class="feature-card">
  <div class="feature-icon"><svg viewBox="0 0 24 24">
    <rect x="3.5" y="3.5" width="6.5" height="6.5" rx="1"/><rect x="14" y="3.5" width="6.5" height="6.5" rx="1"/>
    <rect x="3.5" y="14" width="6.5" height="6.5" rx="1"/><path d="M14 15h3v3h-3zM20.5 14v3M14 20.5h3M20.5 20.5v.01"/>
  </svg></div>
  <h3>Страница записи + QR</h3>
  <p>Ссылка и QR-код для зала — записываются даже те, у кого нет мессенджеров.</p>
</div>
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
<p class="section-lead">Можно ничего не устанавливать и не регистрироваться —
  просто нажать и посмотреть.</p>
<div class="demo-grid">
  <div class="demo-card primary">
    <span class="tag">Полный опыт</span>
    <h3>Демо-бот в Telegram</h3>
    <p>Откройте бота и выберите роль — «Я тренер» или «Я участник». Можно
      создавать тренировки, записываться, отмечать явку — всё как в
      настоящем клубе. Данные сбрасываются каждую ночь.</p>
    <a class="btn-gold" href=\"""" + DEMO_BOT_URL + """\">Открыть бота →</a>
  </div>
  <div class="demo-card">
    <span class="tag">Быстрый взгляд</span>
    <h3>Страница записи</h3>
    <p>Как видит клуб участник без Telegram и ВК — по прямой ссылке
      или QR-коду в зале.</p>
    <a class="btn-ghost" href="/club/1">Открыть страницу →</a>
  </div>
</div>

""" + site_footer() + """
</main>
</body></html>""")
