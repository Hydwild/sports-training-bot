"""
Публичная FAQ-страница: GET /faq.

Тексты вопросов живут в app/api/faq_data.py — здесь только оформление и
сборка. Раньше страница была одной HTML-простынёй, где спортивные и
салонные ответы перемешаны; теперь набор вопросов зависит от выбранного
направления.
"""
from app.api.faq_data import ALL, SECTIONS, VERTICAL_LABELS, items_for
from app.api.public_style import (
    SITE_CSS, TELEGRAM_CONTACT, head_meta, site_footer, site_nav,
)

_EXTRA_CSS = """
.faq-nav{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin:0 0 32px}
.faq-nav a{background:var(--surface);border:1px solid var(--border);color:var(--ink);
  padding:12px 18px;border-radius:999px;text-decoration:none;font:600 13px/1
  -apple-system,system-ui,sans-serif;display:inline-flex;align-items:center;
  min-height:44px}
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
/* JS-режим: плавное раскрытие по max-height. Состоянием управляет класс
   .expanded (не события анимации — нечему «залипнуть»). max-height задаётся
   скриптом по реальной высоте контента, поэтому длинные ответы не обрезаются;
   после раскрытия ставится none (контент может менять высоту). */
details.js-acc .faq-body{overflow:hidden;max-height:0;opacity:0;
  visibility:hidden;
  transition:max-height .32s var(--ease),opacity .25s var(--ease),
    visibility 0s linear .32s}
details.js-acc.expanded .faq-body{opacity:1;visibility:visible;
  transition:max-height .32s var(--ease),opacity .25s var(--ease),
    visibility 0s}
details p, details ol, details ul{margin:0 0 16px;color:var(--ink);
  font:400 14.5px/1.6 -apple-system,system-ui,sans-serif}
details ol, details ul{padding-left:20px}
code{background:var(--surface-2);padding:1px 7px;border-radius:5px;font-size:.92em}

/* переключатель направления: фильтрует вопросы без перезагрузки, а без JS
   ссылки ведут на /faq?v=sport — страница отдаёт уже отфильтрованный набор */
.vpick{display:flex;gap:4px;justify-content:center;margin:0 0 24px;
  background:var(--surface-2);border:1px solid var(--border);border-radius:999px;
  padding:4px;width:fit-content;margin-left:auto;margin-right:auto}
.vpick a{font:600 13px/1 -apple-system,system-ui,sans-serif;text-decoration:none;
  color:var(--muted);padding:10px 18px;border-radius:999px;white-space:nowrap;
  display:inline-flex;align-items:center;min-height:44px}
.vpick a:hover{color:var(--ink)}
.vpick a.on{background:var(--surface);color:var(--ink);box-shadow:var(--shadow)}
h2.group.hidden,details.hidden{display:none}
"""

# Плавное раскрытие/закрытие вопросов. Прогрессивное улучшение: разметка
# остаётся нативной details/summary (без JS работает как раньше), скрипт
# оборачивает ответ в .faq-body>.faq-inner, держит details открытым и
# переключает класс .expanded — весь моушен в CSS (grid-rows), состояние
# не зависит от доставки событий анимации; reduced-motion гасится общим
# правилом в SITE_CSS.


# Переключатель направления. Без JS ссылки ведут на /faq?v=... и сервер
# отдаёт отфильтрованную страницу; со скриптом переключение мгновенное.
_VERTICAL_JS = """<script>
(function(){
  var pick = document.querySelector('.vpick');
  if (!pick) return;
  function apply(v){
    document.querySelectorAll('[data-v]').forEach(function(el){
      var own = el.getAttribute('data-v');
      el.classList.toggle('hidden', v !== 'all' && own !== 'all' && own !== v);
    });
    document.querySelectorAll('h2.group').forEach(function(h){
      var id = h.id, any = false;
      document.querySelectorAll('details[data-s="' + id + '"]').forEach(
        function(d){ if (!d.classList.contains('hidden')) any = true; });
      h.classList.toggle('hidden', !any);
    });
    pick.querySelectorAll('a').forEach(function(a){
      a.classList.toggle('on', a.getAttribute('data-pick') === v);
    });
    history.replaceState(null, '', v === 'all' ? '/faq' : '/faq?v=' + v);
  }
  pick.querySelectorAll('a').forEach(function(a){
    a.addEventListener('click', function(e){
      e.preventDefault();
      apply(a.getAttribute('data-pick'));
    });
  });
})();
</script>"""
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
    d.open = false;
    s.setAttribute('aria-expanded', 'false');
    s.addEventListener('click', function(e){
      e.preventDefault();
      var willOpen = !d.classList.contains('expanded');
      // нативный open включаем ДО анимации открытия и выключаем ПОСЛЕ
      // анимации закрытия — состояние <details> совпадает с видимым,
      // а свёрнутый ответ не читается скринридером и не ловит Tab
      if (willOpen) d.open = true;
      var expanded = d.classList.toggle('expanded');
      s.setAttribute('aria-expanded', expanded ? 'true' : 'false');
      if (expanded) {
        body.style.maxHeight = inner.scrollHeight + 'px';
        // после анимации снимаем ограничение: контент может стать выше
        // (перенос строк при повороте экрана и т.п.)
        setTimeout(function(){
          if (d.classList.contains('expanded')) body.style.maxHeight = 'none';
        }, 340);
      } else {
        // из 'none' анимация не стартует — фиксируем текущую высоту,
        // затем читаем offsetHeight (принудительный reflow, надёжнее
        // requestAnimationFrame) и только потом схлопываем в 0
        body.style.maxHeight = inner.scrollHeight + 'px';
        void body.offsetHeight;
        body.style.maxHeight = '0px';
        setTimeout(function(){
          if (!d.classList.contains('expanded')) d.open = false;
        }, 340);
      }
    });
  });
})();
</script>"""

_HEAD = ("""<!doctype html><html lang="ru"><head>""" + head_meta(
    "Вопросы и ответы — Боты для записей",
    "Как записаться, добавить время, назначить мастера, отметить визит и "
    "оплату — ответы для клиентов, салонов и клубов.",
) + """<style>
""" + SITE_CSS + _EXTRA_CSS + """
</style></head><body>
""" + site_nav("faq") + """
<main class="wrap">

<div class="hero">
  <span class="eyebrow">Вопросы и ответы</span>
  <h1>Как пользоваться ботом записи</h1>
  <p>Для клиентов, салонов красоты, спортивных клубов и частных
    специалистов — коротко и по делу.</p>
</div>
""")

_TAIL = ("""
<p style="text-align:center;margin-top:36px">
  <a class="btn-gold" href=\"""" + TELEGRAM_CONTACT + """\">Остались вопросы — написать в Telegram</a>
</p>

""" + site_footer() + """
</main>
""" + _VERTICAL_JS + _FAQ_JS + """
</body></html>""")


def render_faq_page(vertical: str = ALL) -> str:
    """Страница из FAQ_ITEMS. vertical приходит из ?v= — так переключатель
    работает и без JavaScript."""
    import html as _h

    if vertical not in {v for v, _ in VERTICAL_LABELS}:
        vertical = ALL

    picker = ['<div class="vpick">']
    for value, label in VERTICAL_LABELS:
        href = "/faq" if value == ALL else f"/faq?v={value}"
        on = " on" if value == vertical else ""
        picker.append(f'<a class="{on.strip()}" data-pick="{value}" '
                      f'href="{href}">{label}</a>')
    picker.append("</div>")

    shown = items_for(vertical)
    nav, body = ['<div class="faq-nav">'], []
    for sec_id, sec_title in SECTIONS:
        in_section = [i for i in shown if i.section == sec_id]
        if not in_section:
            continue
        nav.append(f'<a href="#{sec_id}">{sec_title}</a>')
        body.append(f'<h2 class="group" id="{sec_id}">{sec_title}</h2>')
        for item in in_section:
            body.append(
                f'<details data-s="{sec_id}" data-v="{item.vertical}">'
                f"<summary>{_h.escape(item.question)}</summary>"
                f"{item.answer}</details>")
    nav.append("</div>")

    return (_HEAD + "".join(picker) + "".join(nav) + "".join(body)
            + _TAIL)
