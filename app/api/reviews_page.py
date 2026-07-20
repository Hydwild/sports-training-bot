"""
Публичная страница отзывов: GET /reviews (витрина одобренных отзывов +
форма отправки нового), POST /reviews (приём нового отзыва — уходит в
модерацию, на страницу до одобрения не попадает).

Оформление — общее с /promo и /faq (app/api/public_style.py), эта страница
представительская, а не утилитарная (в отличие от /faq и /club/{id}).
"""
from __future__ import annotations

import html as _h

from app.api.public_style import SITE_CSS, TELEGRAM_CONTACT, site_footer, site_nav
from app.models.entities import Review

_HEAD = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Отзывы клиентов — бот записи на тренировки</title><style>
""" + SITE_CSS + """
.stats{display:flex;justify-content:center;gap:40px;margin-top:30px}
.stat b{display:block;font:400 30px/1 Georgia,serif;color:var(--gold);
  font-variant-numeric:tabular-nums}
.stat span{font:600 11px/1.3 -apple-system,system-ui,sans-serif;letter-spacing:.06em;
  text-transform:uppercase;color:#b9b39d}
.divider{display:flex;align-items:center;justify-content:center;gap:12px;
  color:var(--gold);margin:0 auto;max-width:120px}
.divider::before,.divider::after{content:"";height:1px;flex:1;background:var(--border)}
.review-card{display:flex;flex-direction:column}
.stars{color:var(--gold);letter-spacing:2px;font-size:15px;margin-bottom:12px}
.quote{font:400 15.5px/1.6 Georgia,serif;color:var(--ink);flex:1;margin:0 0 16px}
.quote::before{content:"\\201C"}.quote::after{content:"\\201D"}
.who{display:flex;flex-direction:column;border-top:1px solid var(--border);padding-top:12px}
.who b{font:700 13.5px/1.3 -apple-system,system-ui,sans-serif}
.who span{font:400 12.5px/1.3 -apple-system,system-ui,sans-serif;color:var(--muted)}
.empty{text-align:center;color:var(--muted);font:400 15px/1.6 Georgia,serif;
  padding:30px 0}
.form-panel{background:var(--surface);border:1px solid var(--border);border-radius:20px;
  padding:36px 32px;margin-top:54px;box-shadow:var(--shadow);position:relative;
  overflow:hidden}
.form-panel::before{content:"";position:absolute;top:-40%;right:-10%;width:220px;
  height:220px;border-radius:50%;background:radial-gradient(circle,
  color-mix(in srgb, var(--gold) 18%, transparent),transparent 70%);pointer-events:none}
.form-panel h2{margin-top:0}
.rating-pick{display:flex;gap:8px;justify-content:center;margin:6px 0 22px;
  flex-direction:row-reverse;position:relative}
.rating-pick label{cursor:pointer;font-size:28px;color:var(--border);
  transition:color .12s ease,transform .12s ease}
.rating-pick input{position:absolute;opacity:0;pointer-events:none}
.rating-pick input:checked ~ label,.rating-pick label:hover,
.rating-pick label:hover ~ label{color:var(--gold)}
.rating-pick label:hover{transform:scale(1.08)}
.field{margin-bottom:16px;position:relative}
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
  margin-top:6px;position:relative}
button.submit:hover{filter:brightness(1.06)}
.notice{border-radius:14px;padding:16px 18px;margin-top:54px;font:400 14.5px/1.55
  -apple-system,system-ui,sans-serif;text-align:center}
.notice.ok{background:rgba(163,121,44,.12);color:var(--ink);border:1px solid var(--gold)}
.notice.err{background:rgba(178,58,46,.1);color:var(--ink);border:1px solid #b23a2e}
</style></head><body>
"""

_FOOT = "</body></html>"


def _stars(n: int) -> str:
    n = max(1, min(5, n))
    return "★" * n + "☆" * (5 - n)


def render_reviews_page(reviews: list[Review], notice: str | None = None,
                        notice_kind: str = "ok") -> str:
    count = len(reviews)
    avg = (sum(r.rating for r in reviews) / count) if count else 0

    parts = [_HEAD, site_nav("reviews")]
    parts.append('<div class="wrap">')
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
    parts.append(f'<div class="cta-row"><a class="btn-gold" href="#leave">'
                 f'Оставить отзыв</a><a class="btn-ghost" href="{TELEGRAM_CONTACT}">'
                 f'Написать администратору</a></div>')
    parts.append('</div>')

    parts.append('<div class="divider">✦</div>')
    parts.append('<h2 class="section">Истории клиентов</h2>')
    if reviews:
        parts.append('<div class="grid">')
        for r in reviews:
            club = f' · {_h.escape(r.club_name)}' if r.club_name else ''
            parts.append(
                '<div class="card review-card">'
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

    parts.append(site_footer())
    parts.append('</div>')
    parts.append(_FOOT)
    return "".join(parts)
