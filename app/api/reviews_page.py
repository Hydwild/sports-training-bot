"""
Публичная страница отзывов: GET /reviews (витрина одобренных отзывов +
форма отправки нового), POST /reviews (приём нового отзыва — уходит в
модерацию, на страницу до одобрения не попадает).

Оформление сознательно отличается от /faq и /club/{id} (те — утилитарные,
эта — представительская, витрина для потенциальных клиентов).
"""
from __future__ import annotations

import html as _h

from app.models.entities import Review

_HEAD = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Отзывы клиентов — бот записи на тренировки</title><style>
:root{
  --bg:#f6f5f1;--surface:#ffffff;--surface-2:#efece3;--ink:#20211d;--muted:#6b6a60;
  --border:#e4e1d6;--gold:#a3792c;--gold-ink:#2c2007;--ink-hero:#f4f1e6;
  --shadow:0 1px 2px rgba(30,28,20,.05),0 10px 30px rgba(30,28,20,.06);
}
@media (prefers-color-scheme:dark){:root{
  --bg:#141310;--surface:#1c1b17;--surface-2:#232019;--ink:#f1eee2;--muted:#a8a495;
  --border:#302c22;--gold:#d9a94a;--gold-ink:#241a04;--ink-hero:#f4f1e6;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.4);
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:880px;margin:0 auto;padding:0 20px 80px}
.hero{background:radial-gradient(120% 160% at 20% 0%,#332a17,#15120a 70%);
  color:var(--ink-hero);text-align:center;padding:72px 24px 58px;
  border-radius:0 0 28px 28px;margin-bottom:8px}
.hero .eyebrow{display:block;font:700 12px/1 -apple-system,system-ui,sans-serif;
  letter-spacing:.16em;text-transform:uppercase;color:var(--gold);margin-bottom:18px}
.hero h1{font:400 40px/1.2 Georgia,"Times New Roman",serif;letter-spacing:-.01em;
  margin:0 0 16px;text-wrap:balance}
.hero p{max-width:480px;margin:0 auto;font:400 16.5px/1.6 Georgia,serif;
  color:#d9d4c2;opacity:.92}
.stats{display:flex;justify-content:center;gap:36px;margin-top:30px}
.stat b{display:block;font:400 28px/1 Georgia,serif;color:var(--gold)}
.stat span{font:600 11px/1.3 -apple-system,system-ui,sans-serif;letter-spacing:.06em;
  text-transform:uppercase;color:#b9b39d}
h2.section{font:400 26px/1.25 Georgia,serif;text-align:center;margin:52px 0 8px;
  text-wrap:balance}
p.section-lead{text-align:center;color:var(--muted);font:400 15px/1.6 -apple-system,
  system-ui,sans-serif;max-width:52ch;margin:0 auto 30px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;
  padding:24px 22px 20px;box-shadow:var(--shadow);display:flex;flex-direction:column}
.stars{color:var(--gold);letter-spacing:2px;font-size:15px;margin-bottom:12px}
.quote{font:400 15.5px/1.6 Georgia,serif;color:var(--ink);flex:1;margin:0 0 16px}
.quote::before{content:"\\201C"}.quote::after{content:"\\201D"}
.who{display:flex;flex-direction:column;border-top:1px solid var(--border);padding-top:12px}
.who b{font:700 13.5px/1.3 -apple-system,system-ui,sans-serif}
.who span{font:400 12.5px/1.3 -apple-system,system-ui,sans-serif;color:var(--muted)}
.empty{text-align:center;color:var(--muted);font:400 15px/1.6 Georgia,serif;
  padding:30px 0}

.form-panel{background:var(--surface);border:1px solid var(--border);border-radius:20px;
  padding:36px 32px;margin-top:54px;box-shadow:var(--shadow)}
.form-panel h2{margin-top:0}
.rating-pick{display:flex;gap:8px;justify-content:center;margin:6px 0 22px}
.rating-pick label{cursor:pointer;font-size:26px;color:var(--border);
  transition:color .12s ease}
.rating-pick input{position:absolute;opacity:0;pointer-events:none}
.rating-pick input:checked ~ label,.rating-pick label:hover,
.rating-pick label:hover ~ label{color:var(--gold)}
.rating-pick{flex-direction:row-reverse}
.field{margin-bottom:16px}
.field label{display:block;font:600 12.5px/1.3 -apple-system,system-ui,sans-serif;
  letter-spacing:.03em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
input[type=text],textarea{width:100%;padding:12px 14px;border:1px solid var(--border);
  border-radius:10px;background:var(--surface-2);color:var(--ink);font-size:15px;
  font-family:inherit}
textarea{min-height:110px;resize:vertical}
input[type=text]:focus,textarea:focus{outline:2px solid var(--gold);outline-offset:1px}
.hp{position:absolute;left:-9999px;opacity:0}
button.submit{width:100%;padding:14px;border:0;border-radius:10px;background:var(--gold);
  color:var(--gold-ink);font:700 15px/1 -apple-system,system-ui,sans-serif;cursor:pointer;
  margin-top:6px}
button.submit:hover{filter:brightness(1.06)}
.notice{border-radius:14px;padding:16px 18px;margin-top:54px;font:400 14.5px/1.55
  -apple-system,system-ui,sans-serif;text-align:center}
.notice.ok{background:rgba(163,121,44,.12);color:var(--ink);border:1px solid var(--gold)}
.notice.err{background:rgba(178,58,46,.1);color:var(--ink);border:1px solid #b23a2e}
footer{text-align:center;color:var(--muted);padding:34px 0 6px;
  font:400 13px/1.4 -apple-system,system-ui,sans-serif}
footer a{color:var(--gold)}
</style></head><body><div class="wrap">
"""

_FOOT = """
</div></body></html>"""


def _stars(n: int) -> str:
    n = max(1, min(5, n))
    return "★" * n + "☆" * (5 - n)


def render_reviews_page(reviews: list[Review], notice: str | None = None,
                        notice_kind: str = "ok") -> str:
    count = len(reviews)
    avg = (sum(r.rating for r in reviews) / count) if count else 0

    parts = [_HEAD]
    parts.append(
        '<div class="hero"><span class="eyebrow">Отзывы клиентов</span>'
        '<h1>Что говорят тренеры и клубы</h1>'
        '<p>Люди, которые каждый день ведут запись на тренировки через бота — '
        'своими словами о том, что изменилось.</p>')
    if count:
        parts.append(
            '<div class="stats">'
            f'<div class="stat"><b>{avg:.1f}</b><span>средняя оценка</span></div>'
            f'<div class="stat"><b>{count}</b><span>{"отзыв" if count == 1 else "отзывов"}</span></div>'
            '</div>')
    parts.append('</div>')

    parts.append('<h2 class="section">Истории клиентов</h2>')
    if reviews:
        parts.append('<div class="grid">')
        for r in reviews:
            club = f' · {_h.escape(r.club_name)}' if r.club_name else ''
            parts.append(
                '<div class="card">'
                f'<div class="stars">{_stars(r.rating)}</div>'
                f'<p class="quote">{_h.escape(r.text)}</p>'
                f'<div class="who"><b>{_h.escape(r.name)}</b>'
                f'<span>{club.lstrip(" ·")}</span></div>'
                '</div>')
        parts.append('</div>')
    else:
        parts.append(
            '<p class="empty">Отзывов пока нет — станьте первым, кто расскажет '
            'о своём опыте.</p>')

    if notice:
        parts.append(f'<div class="notice {notice_kind}">{_h.escape(notice)}</div>')

    parts.append(
        '<div class="form-panel" id="leave"><h2 class="section" style="margin-top:0">'
        'Оставить отзыв</h2>'
        '<p class="section-lead">После отправки отзыв появится на странице после '
        'проверки — обычно в течение дня.</p>'
        '<form method="post" action="/reviews#leave">'
        '<div class="rating-pick">'
        + "".join(
            f'<input type="radio" name="rating" value="{v}" id="r{v}"'
            f'{" checked" if v == 5 else ""}><label for="r{v}">★</label>'
            for v in (5, 4, 3, 2, 1))
        + '</div>'
        '<div class="field"><label for="name">Ваше имя</label>'
        '<input type="text" id="name" name="name" maxlength="120" required></div>'
        '<div class="field"><label for="club_name">Клуб / город (необязательно)</label>'
        '<input type="text" id="club_name" name="club_name" maxlength="160"></div>'
        '<div class="field"><label for="text">Отзыв</label>'
        '<textarea id="text" name="text" maxlength="1000" required></textarea></div>'
        '<input class="hp" type="text" name="website" tabindex="-1" autocomplete="off">'
        '<button class="submit" type="submit">Отправить отзыв</button>'
        '</form></div>')

    parts.append('<footer>Бот записи на тренировки · '
                 '<a href="/promo">о продукте</a> · <a href="/faq">вопросы и ответы</a>'
                 '</footer>')
    parts.append(_FOOT)
    return "".join(parts)
