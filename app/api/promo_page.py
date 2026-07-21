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
/* тёмная панель в языке hero: фиксированные цвета (фон всегда тёмный,
   тема на него не влияет) */
.trust{background:radial-gradient(120% 160% at 20% 0%,#332a17,#15120a 70%);
  border-radius:20px;padding:32px;display:grid;
  grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:24px}
.trust-item{display:flex;flex-direction:column;align-items:flex-start;gap:10px}
.trust-item svg{width:22px;height:22px;stroke:#d9a94a;fill:none;stroke-width:1.6;
  stroke-linecap:round;stroke-linejoin:round}
.trust-item b{font:600 14px/1.35 -apple-system,system-ui,sans-serif;color:#f4f1e6;
  text-wrap:balance}
.trust-item p{margin:0;font:400 13px/1.55 -apple-system,system-ui,sans-serif;
  color:#b9b39d}
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
.vertical-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
  gap:16px}
.vertical-card{background:var(--surface);border:1px solid var(--border);
  border-radius:16px;padding:24px;box-shadow:var(--shadow)}
.vertical-card .vi{width:40px;height:40px;border-radius:12px;
  border:1px solid var(--gold);display:flex;align-items:center;
  justify-content:center;margin-bottom:16px}
.vertical-card .vi svg{width:19px;height:19px;stroke:var(--gold);fill:none;
  stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.vertical-card h3{font:400 17.5px/1.35 Georgia,serif;margin:0 0 8px}
.vertical-card p{margin:0;font:400 14px/1.6 -apple-system,system-ui,sans-serif;
  color:var(--muted)}
.vertical-card .tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}
.vertical-card .tags span{font:600 11.5px/1 -apple-system,system-ui,sans-serif;
  color:var(--muted);border:1px solid var(--border);border-radius:999px;
  padding:6px 10px}
"""

PROMO_HTML = ("""<!doctype html><html lang="ru"><head>""" + head_meta(
    "Боты для записей — Telegram и ВКонтакте",
    "Онлайн-запись для спортивных клубов и салонов красоты: Telegram, "
    "ВКонтакте и сайт. Очередь, напоминания, оплата, статистика. Живое демо.",
) + """<style>
""" + SITE_CSS + _EXTRA_CSS + """
</style></head><body>
""" + site_nav("promo") + """
<main class="wrap">

<div class="hero">
  <span class="eyebrow">Telegram · ВКонтакте · веб-страница записи</span>
  <h1>Пока вы работаете —<br>запись ведёт себя сама</h1>
  <p>Клиенты записываются сами, встают в очередь и получают напоминания.
    Подходит спортивным клубам, секциям и салонам красоты.</p>
  <div class="cta-row">
    <a class="btn-gold" href="#demo">Живое демо</a>
    <a class="btn-ghost" href="#price">Как подключить</a>
  </div>
</div>

<h2 class="section">Под ваш бизнес</h2>
<p class="section-lead">Терминология, кнопки бота и страница записи
  настраиваются под направление — клиент видит привычные слова.</p>
<div class="vertical-grid">
<div class="vertical-card">
  <div class="vi"><svg viewBox="0 0 24 24">
    <path d="M6.5 6.5v11M17.5 6.5v11M3.5 9v6M20.5 9v6M6.5 12h11"/>
  </svg></div>
  <h3>Спорт и тренировки</h3>
  <p>Групповые и персональные тренировки: запись, очередь на место,
    явка, рейтинг посещаемости.</p>
  <div class="tags"><span>клубы</span><span>секции</span>
    <span>персональные тренеры</span><span>аренда кортов</span></div>
</div>
<div class="vertical-card">
  <div class="vi"><svg viewBox="0 0 24 24">
    <circle cx="6.5" cy="6.5" r="2.8"/><circle cx="6.5" cy="17.5" r="2.8"/>
    <path d="M9 8.5L20.5 20M9 15.5L20.5 4"/>
  </svg></div>
  <h3>Салоны красоты</h3>
  <p>Индивидуальные окна записи к мастеру: имя, фото и специализация
    мастера прямо на странице записи.</p>
  <div class="tags"><span>барберы</span><span>маникюр</span>
    <span>волосы</span><span>депиляция</span><span>косметология</span></div>
</div>
<div class="vertical-card">
  <div class="vi"><svg viewBox="0 0 24 24">
    <circle cx="12" cy="12" r="8.5"/><path d="M12 8v4l2.5 2.5"/>
  </svg></div>
  <h3>Любые услуги по записи</h3>
  <p>Репетиторы, мастер-классы, студии, консультации — всё, где клиент
    выбирает время.</p>
  <div class="tags"><span>репетиторы</span><span>студии</span>
    <span>мастер-классы</span></div>
</div>
</div>

<h2 class="section">Что умеет</h2>
<p class="section-lead">Всё, что обычно решают табличкой, перепиской и терпением.</p>
<div class="feature-grid">
<div class="feature-card">
  <div class="feature-icon"><svg viewBox="0 0 24 24"><path d="M5 13l4 4L19 7"/></svg></div>
  <h3>Запись в один клик</h3>
  <p>Из Telegram, ВК или браузера — клиенту не нужно ничего устанавливать.
    Очередь при заполнении мест, автоматический подъём при отмене.</p>
</div>
<div class="feature-card">
  <div class="feature-icon"><svg viewBox="0 0 24 24">
    <rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/>
  </svg></div>
  <h3>Автопилот расписания</h3>
  <p>Задайте «вторник и четверг 19:00» — слоты создаются сами,
    подписчики получают уведомление об открытии записи.</p>
</div>
<div class="feature-card">
  <div class="feature-icon"><svg viewBox="0 0 24 24">
    <path d="M6.5 9a5.5 5.5 0 0111 0c0 5.5 2 6.5 2 6.5h-15s2-1 2-6.5z"/><path d="M10.3 19.5a1.9 1.9 0 003.4 0"/>
  </svg></div>
  <h3>Напоминания</h3>
  <p>Напоминание за выбранное время до визита — меньше неявок и путаницы.</p>
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
  <p>Посещаемость, личный профиль клиента, история визитов — всё
    считается само.</p>
</div>
<div class="feature-card">
  <div class="feature-icon"><svg viewBox="0 0 24 24">
    <rect x="3.5" y="3.5" width="6.5" height="6.5" rx="1"/><rect x="14" y="3.5" width="6.5" height="6.5" rx="1"/>
    <rect x="3.5" y="14" width="6.5" height="6.5" rx="1"/><path d="M14 15h3v3h-3zM20.5 14v3M14 20.5h3M20.5 20.5v.01"/>
  </svg></div>
  <h3>Страница записи + QR</h3>
  <p>Ссылка и QR-код для зала или салона — записываются даже те, у кого
    нет мессенджеров.</p>
</div>
</div>

<h2 class="section">Как это работает</h2>
<div class="card" style="max-width:640px;margin:0 auto">
<div class="steps">
<div class="step"><div><b>Разворачиваем бота под ваш бизнес</b>
  <p>Ваше название, ваши боты, ваши цвета — готово за один день.</p></div></div>
<div class="step"><div><b>Даёте клиентам ссылку</b>
  <p>Они записываются сами — из Telegram, ВК или по QR на месте.</p></div></div>
<div class="step"><div><b>Работаете, а не администрируете</b>
  <p>Расписание, напоминания и списки клиентов бот ведёт сам.</p></div></div>
</div>
</div>

<h2 class="section">Надёжность и данные</h2>
<p class="section-lead">Инженерная часть, о которой не нужно думать —
  она просто работает.</p>
<div class="trust">
  <div class="trust-item">
    <svg viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="7.5" ry="3"/>
      <path d="M4.5 6v12c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3V6"/>
      <path d="M4.5 12c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3"/></svg>
    <b>Ежедневные бэкапы</b>
    <p>Копия базы хранится вне сервера и переживает даже падение хостинга.</p>
  </div>
  <div class="trust-item">
    <svg viewBox="0 0 24 24"><rect x="5.5" y="10.5" width="13" height="9" rx="2"/>
      <path d="M8.5 10.5V8a3.5 3.5 0 017 0v2.5"/></svg>
    <b>Изоляция данных</b>
    <p>Данные каждого салона и клуба технически недоступны другим —
      клиенты, записи и выручка не пересекаются.</p>
  </div>
  <div class="trust-item">
    <svg viewBox="0 0 24 24"><path d="M3.5 12h4l2.5-6 4 12 2.5-6h4"/></svg>
    <b>Уведомления о сбоях</b>
    <p>Сбой фоновой задачи или бэкапа — и владельцу приходит сообщение
      в Telegram.</p>
  </div>
  <div class="trust-item">
    <svg viewBox="0 0 24 24">
      <path d="M12 3.5l7 2.5v5c0 4.5-3 8-7 9.5-4-1.5-7-5-7-9.5v-5z"/>
      <path d="M9 12l2 2 4-4.5"/></svg>
    <b>Проверка перед выкаткой</b>
    <p>Более 200 автотестов на запись, оплату и уведомления — прогоняются
      перед обновлением.</p>
  </div>
</div>

<h2 class="section" id="price">Тариф под задачу</h2>
<p class="section-lead">От одного мастера или тренера до клуба с несколькими
  группами и мастерами — редакция под ваш масштаб.</p>
<div class="price-card">
  <span class="tag">Свой бот под ключ</span>
  <h3>Настройка под ваш бизнес + запуск + инструкция</h3>
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
  <!--DEMO_CLUB_CARD-->
</div>

""" + site_footer() + """
</main>
</body></html>""")

# Карточка «Страница записи» показывается ТОЛЬКО если есть демо-клуб
# (Tenant.is_demo). Раньше здесь была ссылка на /club/1 — страницу
# настоящего заказчика: посторонние попадали на чужой клуб, а выглядел он
# как заброшенное демо. Лучше не показать карточку вовсе, чем показать чужое.
_DEMO_CLUB_SLOT = "<!--DEMO_CLUB_CARD-->"


def render_promo_page(demo_club_id: int | None) -> str:
    if demo_club_id is None:
        return PROMO_HTML.replace(_DEMO_CLUB_SLOT, "")
    card = (
        '<div class="demo-card">'
        '<span class="tag">Быстрый взгляд</span>'
        '<h3>Страница записи</h3>'
        '<p>Как видит клуб участник без Telegram и ВК — по прямой ссылке '
        'или QR-коду в зале.</p>'
        f'<a class="btn-ghost" href="/club/{demo_club_id}">'
        'Открыть страницу →</a>'
        '</div>')
    return PROMO_HTML.replace(_DEMO_CLUB_SLOT, card)
