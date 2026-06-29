"""Общие тексты сообщений (одинаковые для Telegram и VK)."""
from __future__ import annotations

from app.services.booking import BookingService


async def training_card(svc: BookingService, t) -> str:
    active = await svc.repo.get_signups(t.id, "active")
    queue = await svc.repo.get_signups(t.id, "queue")
    lines = [f"🏸 {t.title}", f"📅 {svc.format_local(t.start_at)}"]
    if t.location:
        lines.append(f"📍 {t.location}")
    h = t.duration_min / 60
    lines.append(f"⏱ {('%.1f' % h).rstrip('0').rstrip('.')} ч")
    lines.append(f"👥 {len(active)}/{t.max_participants}")
    if t.state == "draft":
        if t.publish_at:
            lines.append(f"📝 Черновик — запись откроется {svc.format_local(t.publish_at)}")
        else:
            lines.append("📝 Черновик — запись не открыта")
    if active:
        lines.append("\nЗаписаны:")
        lines += [f"  {i}. {_label(s)}" for i, s in enumerate(active, 1)]
    if queue:
        lines.append("\nОчередь:")
        lines += [f"  {i}. {_label(s)}" for i, s in enumerate(queue, 1)]
    return "\n".join(lines)


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
