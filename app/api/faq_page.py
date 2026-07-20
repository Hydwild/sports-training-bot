"""Публичная FAQ-страница для клиентов: GET /faq. Правьте текст здесь."""
from app.api.public_style import (
    SITE_CSS, TELEGRAM_CONTACT, head_meta, site_footer, site_nav,
)

_EXTRA_CSS = """
.faq-nav{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin:0 0 32px}
.faq-nav a{background:var(--surface);border:1px solid var(--border);color:var(--ink);
  padding:12px 18px;border-radius:999px;text-decoration:none;font:600 13px/1
  -apple-system,system-ui,sans-serif}
.faq-nav a:hover{border-color:var(--gold)}
h2.group{font:400 22px/1.3 Georgia,serif;margin:48px 0 16px;text-align:left}
details{background:var(--surface);border:1px solid var(--border);border-radius:16px;
  padding:4px 20px;margin-bottom:12px;box-shadow:var(--shadow);
  transition:border-color .2s var(--ease)}
details[open]:not(.js-acc),details.expanded{border-color:var(--border-hover)}
summary{cursor:pointer;padding:16px 0;font:600 14.5px/1.45 -apple-system,system-ui,
  sans-serif;list-style:none;display:flex;align-items:center;
  justify-content:space-between;gap:16px}
summary:hover{color:var(--gold)}
summary::-webkit-details-marker{display:none}
summary::after{content:"";width:9px;height:9px;flex-shrink:0;position:relative;
  top:-2px;border-right:2px solid var(--gold);border-bottom:2px solid var(--gold);
  border-radius:1px;transform:rotate(45deg);
  transition:transform .3s var(--ease)}
details[open]:not(.js-acc) summary::after,
details.expanded summary::after{transform:rotate(225deg);top:2px}
/* JS-режим: плавное раскрытие через grid-rows 0fr->1fr. Состоянием управляет
   класс .expanded (не события анимации — нечему «залипнуть»); visibility
   убирает свёрнутые ответы из фокуса и дерева доступности. */
details.js-acc .faq-body{display:grid;grid-template-rows:0fr;opacity:0;
  visibility:hidden;
  transition:grid-template-rows .32s var(--ease),opacity .25s var(--ease),
    visibility .32s}
details.js-acc.expanded .faq-body{grid-template-rows:1fr;opacity:1;
  visibility:visible}
details.js-acc .faq-inner{overflow:hidden;min-height:0}
details p, details ol, details ul{margin:0 0 16px;color:var(--ink);
  font:400 14.5px/1.6 -apple-system,system-ui,sans-serif}
details ol, details ul{padding-left:20px}
code{background:var(--surface-2);padding:1px 7px;border-radius:5px;font-size:.92em}
"""

# Плавное раскрытие/закрытие вопросов. Прогрессивное улучшение: разметка
# остаётся нативной details/summary (без JS работает как раньше), скрипт
# оборачивает ответ в .faq-body>.faq-inner, держит details открытым и
# переключает класс .expanded — весь моушен в CSS (grid-rows), состояние
# не зависит от доставки событий анимации; reduced-motion гасится общим
# правилом в SITE_CSS.
_FAQ_JS = """<script>
(function(){
  document.querySelectorAll('details').forEach(function(d){
    var s = d.querySelector('summary');
    var body = document.createElement('div');
    var inner = document.createElement('div');
    body.className = 'faq-body'; inner.className = 'faq-inner';
    Array.prototype.slice.call(d.children).forEach(function(c){
      if (c.tagName !== 'SUMMARY') inner.appendChild(c);
    });
    body.appendChild(inner);
    d.appendChild(body);
    d.classList.add('js-acc');
    d.open = true;
    s.setAttribute('aria-expanded', 'false');
    s.addEventListener('click', function(e){
      e.preventDefault();
      var expanded = d.classList.toggle('expanded');
      s.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    });
  });
})();
</script>"""

FAQ_HTML = ("""<!doctype html><html lang="ru"><head>""" + head_meta(
    "Вопросы и ответы — Боты для записей",
    "Как записаться на тренировку, создать расписание, отметить явку и "
    "оплату — ответы на частые вопросы для участников и тренеров.",
) + """<style>
""" + SITE_CSS + _EXTRA_CSS + """
</style></head><body>
""" + site_nav("faq") + """
<main class="wrap">

<div class="hero">
  <span class="eyebrow">Вопросы и ответы</span>
  <h1>Как пользоваться ботом записи</h1>
  <p>Для участников и для тренеров — коротко и по делу.</p>
</div>

<div class="faq-nav">
  <a href="#participants">Для участников</a>
  <a href="#trainers">Для тренеров</a>
  <a href="#tech">Технические вопросы</a>
</div>

<h2 class="group" id="participants">Для участников</h2>

<details>
<summary>Как записаться на тренировку?</summary>
<p>В Telegram/ВК нажмите «Тренировки» в меню бота — увидите ближайшие
занятия с кнопкой «✅ Записаться». Если мест нет — кнопка предложит встать
в очередь, при освобождении места вы подниметесь автоматически и получите
уведомление.</p>
<p>Без Telegram и ВК — попросите тренера ссылку на страницу записи
(вида <code>.../club/1</code>), запись там по имени и телефону, без
регистрации.</p>
</details>

<details>
<summary>Как посмотреть свои записи или отменить их?</summary>
<p>«📅 Мои записи» в меню бота — список тренировок, куда вы записаны, с
кнопкой отмены рядом с каждой. Если тренер включил «окно отмены» —
отписаться в последние N минут перед тренировкой не получится, бот
объяснит это в ответе.</p>
</details>

<details>
<summary>Что такое рейтинг и профиль?</summary>
<p>«🏆 Рейтинг» — топ посещаемости клуба. «👤 Профиль» — ваша личная
статистика: сколько тренировок посетили, сколько часов наиграли,
пропуски и неоплаченные посещения. Статистика считается по отметкам
явки, которые ставит тренер.</p>
</details>

<h2 class="group" id="trainers">Для тренеров</h2>

<details>
<summary>Как создать тренировку?</summary>
<p>«➕ Создать тренировку» в меню бота — пошаговый диалог: название, дата и
время, место, лимит участников, длительность, цена (можно бесплатно).
В конце выбираете: открыть запись сразу, оставить черновиком или задать
время автопубликации.</p>
</details>

<details>
<summary>Можно ли не создавать тренировки каждый раз вручную?</summary>
<p>Да — «📆 Расписание»: задайте день недели и время (например «вторник и
четверг 19:00»), и бот будет сам создавать тренировки заранее (за
настроенное число дней) и оповещать подписчиков об открытии записи.</p>
</details>

<details>
<summary>Как отметить явку и оплату?</summary>
<p>«✅ Явки» в меню бота — выберите тренировку, у каждого участника
отметка явки и оплаты в один тап. В веб-админке (если у вас Pro) то же
самое доступно в виде таблицы по ссылке из тренировки.</p>
</details>

<details>
<summary>Как узнать, кто должен за прошедшие тренировки?</summary>
<p>Команда/кнопка «Должники» показывает участников с неоплаченными
посещениями. Отдельно можно разослать им напоминание об оплате.</p>
</details>

<details>
<summary>Участник пришёл, но не может записаться сам (нет Telegram/ВК)?</summary>
<p>«👤 Записать гостя» — вы вносите его вручную по имени. Запись помечается
неподтверждённой, пока вы не подтвердите её тем же действием — так
гость не занимает место «навсегда», если не пришёл.</p>
</details>

<details>
<summary>Как разослать сообщение всем подписчикам?</summary>
<p>«📢 Рассылка» — вводите текст, бот отправляет его всем, кто хоть раз
взаимодействовал с ботом клуба (и в Telegram, и в ВК, если подключены
оба).</p>
</details>

<details>
<summary>Как выгрузить список участников (для печати/бухгалтерии)?</summary>
<p>В карточке тренировки — экспорт в Excel, PDF или CSV. В веб-админке
(Pro) — те же форматы кнопками на странице тренировки.</p>
</details>

<details>
<summary>Как получить ссылку и QR-код для зала?</summary>
<p>В веб-админке (Pro) на дашборде — «Публичная страница записи» и рядом
«QR-код для печати». По этой ссылке записываются даже те, у кого нет
Telegram/ВК.</p>
</details>

<details>
<summary>Что такое веб-админка и как туда войти?</summary>
<p>Доступна в редакции Pro по адресу <code>/admin</code> — вход через
Telegram (без пароля, кнопка «Войти через Telegram»). Там: список
тренировок, участники, экспорт, настройки клуба (брендинг, напоминания),
скачивание бэкапа базы. Доступ получают только тренеры и ассистенты,
которых владелец клуба добавил в состав.</p>
</details>

<details>
<summary>Можно ли поменять цвет/название клуба в сообщениях бота?</summary>
<p>Да — в веб-админке «⚙️ Настройки клуба»: название, цвет акцента,
логотип, а также включение/выключение напоминаний и их время.</p>
</details>

<details>
<summary>Как настроить напоминания о тренировке?</summary>
<p>В настройках клуба — включить напоминания и задать, за сколько минут
до начала их отправлять. Отдельно можно настроить напоминание тренеру о
неподтверждённых гостях и автоснятие гостей, которых не подтвердили.</p>
</details>

<h2 class="group" id="tech">Технические вопросы</h2>

<details>
<summary>Бот не отвечает — что делать?</summary>
<p>Подождите минуту-две — если был кратковременный сбой сети, бот
автоматически переподключится сам. Если не отвечает дольше 10 минут —
напишите администратору платформы, он получает автоматические
уведомления о сбоях и обычно уже в курсе.</p>
</details>

<details>
<summary>Что будет, если закончится оплата подписки?</summary>
<p>Вы получите уведомление в бота заранее (за 3 дня) и в день истечения.
После истечения бот и страница записи отвечают «работа приостановлена» —
участники не могут записываться, но все данные (тренировки, история,
статистика) сохраняются и восстанавливаются сразу после продления.</p>
</details>

<details>
<summary>Насколько надёжно хранятся данные?</summary>
<p>База резервируется ежедневно автоматически, копия хранится отдельно от
основного сервера. Данные разных клубов полностью изолированы друг от
друга технически — один клуб не может увидеть данные другого.</p>
</details>

<details>
<summary>Можно ли перенести бота на свою инфраструктуру?</summary>
<p>Да, это отдельная услуга — обратитесь к администратору платформы для
уточнения условий.</p>
</details>

<p style="text-align:center;margin-top:36px">
  <a class="btn-gold" href=\"""" + TELEGRAM_CONTACT + """\">Остались вопросы — написать в Telegram</a>
</p>

""" + site_footer() + """
</main>
""" + _FAQ_JS + """
</body></html>""")
