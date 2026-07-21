"""
Общий визуальный язык для публичных представительских страниц —
/promo, /faq, /reviews (НЕ для /club/{id}, та утилитарная и осознанно
проще). Единая палитра, типографика, метатеги (Open Graph — превью ссылок
в Telegram/ВК) и навигация между тремя страницами, чтобы они читались
как один сайт, а не три случайных экрана.

Ритм отступов — 8pt-сетка (8/16/24/32/48/64), кривая анимаций --ease
(iOS-подобная), интерактив с press-feedback и тач-целями ≥44px.
"""
from __future__ import annotations

TELEGRAM_CONTACT = "https://t.me/NeoMeal"

# noise.png-подобная зернистость через SVG feTurbulence — без внешних
# файлов/шрифтов (CSP/офлайн-надёжность), едва заметная, только на hero.
_GRAIN = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg'><filter id='n'>"
    "<feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' "
    "stitchTiles='stitch'/></filter>"
    "<rect width='100%25' height='100%25' filter='url(%23n)'/></svg>"
)

# Фавикон: тёмная плашка с золотой галочкой — та же палитра, что hero и
# line-иконки на /promo. Inline data-URI, внешних файлов нет.
_FAVICON = (
    "data:image/svg+xml,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='14' fill='%2315120a'/>"
    "<path d='M20 34l8 8 16-17' stroke='%23d9a94a' stroke-width='6.5' "
    "fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>"
)


def head_meta(title: str, description: str) -> str:
    """Единый head-блок: описание, Open Graph (по нему Telegram/ВК строят
    превью ссылки — основной канал, где сайтом будут делиться), фавикон и
    theme-color для адресной строки мобильных браузеров. Значения статичные,
    не из пользовательского ввода — экранирование не требуется."""
    return "".join([
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{title}</title>",
        f'<meta name="description" content="{description}">',
        f'<meta property="og:title" content="{title}">',
        f'<meta property="og:description" content="{description}">',
        '<meta property="og:type" content="website">',
        '<meta property="og:locale" content="ru_RU">',
        '<meta property="og:site_name" content="Боты для записей">',
        '<meta name="twitter:card" content="summary">',
        '<meta name="theme-color" content="#f6f5f1" media="(prefers-color-scheme: light)">',
        '<meta name="theme-color" content="#141310" media="(prefers-color-scheme: dark)">',
        f'<link rel="icon" type="image/svg+xml" href="{_FAVICON}">',
    ])


SITE_CSS = """
:root{
  --bg:#f6f5f1;--surface:#ffffff;--surface-2:#efece3;--ink:#20211d;--muted:#65645a;
  --border:#e4e1d6;--border-hover:#cdbd97;--gold:#a3792c;--gold-ink:#2c2007;
  --gold-soft:rgba(163,121,44,.12);--selection:#eadfc2;--ink-hero:#f4f1e6;
  --bg-glass:rgba(246,245,241,.82);
  --shadow:0 1px 2px rgba(30,28,20,.05),0 10px 30px rgba(30,28,20,.06);
  --ease:cubic-bezier(.32,.72,.33,1);
}
@media (prefers-color-scheme:dark){:root{
  --bg:#141310;--surface:#1c1b17;--surface-2:#232019;--ink:#f1eee2;--muted:#a8a495;
  --border:#302c22;--border-hover:#584a2b;--gold:#d9a94a;--gold-ink:#241a04;
  --gold-soft:rgba(217,169,74,.14);--selection:#4a3d1f;--ink-hero:#f4f1e6;
  --bg-glass:rgba(20,19,16,.8);
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.4);
}}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
.wrap{max-width:880px;margin:0 auto;padding:0 20px 96px}
a{color:inherit}
[id]{scroll-margin-top:88px}
::selection{background:var(--selection)}
:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
a,button,summary{transition:color .15s var(--ease),border-color .15s var(--ease),
  background-color .15s var(--ease),box-shadow .15s var(--ease),
  filter .15s var(--ease),transform .15s var(--ease);
  -webkit-tap-highlight-color:transparent;touch-action:manipulation}
@media (prefers-reduced-motion:reduce){
  html{scroll-behavior:auto}
  *,*::before,*::after{transition:none!important;animation:none!important}
}

/* ---------- навигация: закреплённая полоса с блюром (iOS-стиль) ---------- */
.nav-bar{position:sticky;top:0;z-index:100;background:var(--bg-glass);
  -webkit-backdrop-filter:blur(14px) saturate(1.5);
  backdrop-filter:blur(14px) saturate(1.5);
  border-bottom:1px solid var(--border)}
.site-nav{
  display:flex;align-items:center;justify-content:space-between;gap:16px;
  max-width:880px;margin:0 auto;padding:12px 20px;
}
.site-nav .brand{font:400 17px/1 Georgia,serif;letter-spacing:.01em;
  text-decoration:none;color:var(--ink);display:inline-flex;align-items:center;
  min-height:44px}
.site-nav .brand b{color:var(--gold);font-weight:400}
.site-nav .links{display:flex;gap:4px;background:var(--surface-2);
  border:1px solid var(--border);border-radius:999px;padding:4px}
.site-nav .links a{
  font:600 13px/1 -apple-system,system-ui,sans-serif;text-decoration:none;
  color:var(--muted);padding:10px 16px;border-radius:999px;white-space:nowrap;
  display:inline-flex;align-items:center;min-height:44px;
}
.site-nav .links a:hover{color:var(--ink)}
.site-nav .links a.on{background:var(--surface);color:var(--ink);box-shadow:var(--shadow)}
@media (max-width:640px){
  .nav-bar{position:static}
  .site-nav{flex-direction:column;align-items:stretch;gap:12px;padding:16px 20px 12px}
  .site-nav .brand{text-align:center}
  .site-nav .links{justify-content:center}
}

/* ---------- hero (общий стиль на всех трёх страницах) ---------- */
.hero{
  position:relative;overflow:hidden;
  background:radial-gradient(120% 160% at 20% 0%,#332a17,#15120a 70%);
  color:var(--ink-hero);text-align:center;padding:64px 24px 56px;
  border-radius:0 0 28px 28px;margin:16px 0 8px;isolation:isolate;
}
.hero::after{
  content:"";position:absolute;inset:0;background-image:url("GRAIN_URI");
  opacity:.05;mix-blend-mode:overlay;pointer-events:none;z-index:-1;
}
.hero .eyebrow{display:block;font:700 12px/1 -apple-system,system-ui,sans-serif;
  letter-spacing:.16em;text-transform:uppercase;color:var(--gold);margin-bottom:16px}
.hero h1{font:400 40px/1.2 Georgia,"Times New Roman",serif;letter-spacing:-.01em;
  margin:0 0 16px;text-wrap:balance}
.hero p{max-width:500px;margin:0 auto;font:400 16.5px/1.6 Georgia,serif;
  color:#d9d4c2;opacity:.92}
.hero .cta-row{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-top:32px}

/* ---------- кнопки: тач-цель ≥44px, press-feedback ---------- */
.btn-gold,.btn-ghost{
  display:inline-flex;align-items:center;gap:6px;padding:15px 28px;border-radius:999px;
  font:700 14.5px/1 -apple-system,system-ui,sans-serif;text-decoration:none;
  border:1px solid transparent;cursor:pointer;
}
.btn-gold{background:var(--gold);color:var(--gold-ink)}
.btn-gold:hover{filter:brightness(1.06)}
.btn-ghost{background:transparent;color:var(--ink-hero);border-color:rgba(244,241,230,.35)}
.btn-ghost:hover{border-color:rgba(244,241,230,.7)}
.btn-gold:active,.btn-ghost:active{transform:scale(.97)}

/* ---------- секции/заголовки: ритм 64/8/32 ---------- */
h2.section{font:400 26px/1.25 Georgia,serif;text-align:center;margin:64px 0 8px;
  text-wrap:balance}
p.section-lead{text-align:center;color:var(--muted);font:400 15px/1.6 -apple-system,
  system-ui,sans-serif;max-width:52ch;margin:0 auto 32px}

/* ---------- карточки ---------- */
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;
  padding:24px;box-shadow:var(--shadow)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px}

/* ---------- footer ---------- */
footer.site{text-align:center;color:var(--muted);padding:48px 0 8px;
  font:400 13px/1.4 -apple-system,system-ui,sans-serif;border-top:1px solid var(--border);
  margin-top:24px}
footer.site .links{margin-bottom:12px}
footer.site a{color:var(--gold);text-decoration:none;font-weight:600;
  margin:0 4px;display:inline-flex;align-items:center;min-height:44px;
  padding:0 4px}
footer.site a:hover{text-decoration:underline}
""".replace("GRAIN_URI", _GRAIN)


def site_nav(active: str) -> str:
    """active: 'promo' | 'faq' | 'reviews'"""
    items = [("promo", "/promo", "О продукте"),
            ("faq", "/faq", "Вопросы и ответы"),
            ("reviews", "/reviews", "Отзывы")]
    links = "".join(
        f'<a class="on" aria-current="page" href="{href}">{label}</a>'
        if key == active else f'<a href="{href}">{label}</a>'
        for key, href, label in items)
    return (
        '<div class="nav-bar"><nav class="site-nav" aria-label="Разделы сайта">'
        '<a class="brand" href="/promo"><b>Боты</b> для записей</a>'
        f'<div class="links">{links}</div>'
        '</nav></div>')


def site_footer() -> str:
    return (
        f'<footer class="site"><div class="links">'
        f'<a href="/promo">о продукте</a>·<a href="/faq">вопросы и ответы</a>'
        f'·<a href="/reviews">отзывы</a>·<a href="{TELEGRAM_CONTACT}">написать в Telegram</a>'
        f'</div>© 2026 · Боты для записей — Telegram + ВКонтакте</footer>')
