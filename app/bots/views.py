"""Общие тексты сообщений (одинаковые для Telegram и VK)."""
from __future__ import annotations

from app.services.booking import BookingService


async def training_card(svc: BookingService, t) -> str:
    active = await svc.repo.get_signups(t.id, "active")
    queue = await svc.repo.get_signups(t.id, "queue")

    # прогресс-бар заполнения мест
    filled = len(active)
    total = t.max_participants
    bar = _progress_bar(filled, total)

    lines = [f"🏸 <b>{t.title}</b>", f"📅 {svc.format_local(t.start_at)}"]
    if t.location:
        lines.append(f"📍 {t.location}")
    h = t.duration_min / 60
    lines.append(f"⏱ {('%.1f' % h).rstrip('0').rstrip('.')} ч")
    lines.append(f"👥 {filled}/{total}  {bar}")

    if t.state == "draft":
        if t.publish_at:
            lines.append(f"📝 <i>Черновик — запись откроется {svc.format_local(t.publish_at)}</i>")
        else:
            lines.append("📝 <i>Черновик — запись не открыта</i>")

    if active:
        lines.append("\n<b>Записаны:</b>")
        lines += [f"  {i}. {_label(s)}" for i, s in enumerate(active, 1)]
    if queue:
        lines.append("\n<b>⏳ Очередь:</b>")
        lines += [f"  {i}. {_label(s)}" for i, s in enumerate(queue, 1)]
    if not active and not queue:
        lines.append("\n<i>Пока никто не записан — будь первым!</i>")
    return "\n".join(lines)


def _progress_bar(filled: int, total: int, width: int = 10) -> str:
    """Текстовый прогресс-бар заполнения мест: ▰▰▰▱▱▱▱▱▱▱"""
    if total <= 0:
        return ""
    ratio = min(1.0, filled / total)
    full = round(ratio * width)
    return "▰" * full + "▱" * (width - full)


def _label(s) -> str:
    """Имя участника с @username (если есть) и пометкой гостя."""
    if getattr(s, "is_guest", False):
        mark = "✅" if s.confirmed else "⏳ требует подтверждения"
        return f"{s.name} (гость, {mark})"
    uname = getattr(s, "username", None)
    suffix = f" @{uname}" if uname else ""
    return f"{s.name}{suffix}"


def signup_result(res, title: str) -> str:
    return {
        "active": "✅ Вы записаны!",
        "queue": f"⏳ Основной состав заполнен. Очередь №{res.position}. "
                 f"Освободится место — поднимем автоматически.",
        "already": "Вы уже записаны.",
        "closed": "Запись закрыта или тренировка отменена.",
    }[res.result]


def profile_card(name: str, stats: dict) -> str:
    lines = [f"👤 Профиль: {name}", "",
             f"✅ Посещено тренировок: {stats['attended']}",
             f"⏱ Наиграно часов: {stats['hours']}",
             f"📋 Всего записей (прошедшие): {stats['signups']}"]
    if stats["missed"]:
        lines.append(f"🚫 Пропущено: {stats['missed']}")
    if stats["unpaid"]:
        lines.append(f"💰 Неоплачено посещений: {stats['unpaid']}")
    if stats["attended"] == 0:
        lines.append("\nСтатистика появится после отметок явки админом.")
    return "\n".join(lines)


def ranking_text(rows: list[dict]) -> str:
    if not rows:
        return "Пока нет данных о посещениях."
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["🏆 Рейтинг посещаемости:\n"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{medals.get(i, f'{i}.')} {r['name']} — "
                     f"{r['attended']} трен., {r['hours']} ч")
    return "\n".join(lines)


def attendance_summary(svc: BookingService, t, summ: dict) -> str:
    return (f"📊 «{t.title}» ({svc.format_local(t.start_at)})\n"
            f"Записано: {summ['signed']} | Пришло: {summ['attended']} | "
            f"Оплатило: {summ['paid']} | Долгов: {summ['unpaid']}")


def debtors_text(debtors: list[dict]) -> str:
    if not debtors:
        return "💸 Должников нет — все посещения оплачены."
    lines = ["💰 Должники:\n"]
    for d in debtors:
        lines.append(f"• {d['name']} — {d['debts']} трен.")
    lines.append(f"\nВсего: {len(debtors)}.")
    return "\n".join(lines)
