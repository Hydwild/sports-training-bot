"""Общие тексты сообщений (одинаковые для Telegram и VK)."""
from __future__ import annotations

import html as _html

from app.services.booking import BookingService


async def training_card(svc: BookingService, t, for_admin: bool = False) -> str:
    """
    HTML-карточка для Telegram (parse_mode="HTML"). Заголовок/место задаёт
    тренер (доверенная роль), но имена участников — из профиля Telegram,
    который может содержать произвольные символы включая теги (`<a href=...>`
    и т.п.). Экранируем ВСЁ, что не является нашей собственной разметкой,
    иначе участник может внедрить кликабельную ссылку/форматирование в
    карточку, которую видят все в группе.
    """
    active = await svc.repo.get_signups(t.id, "active")
    queue = await svc.repo.get_signups(t.id, "queue")

    # подписи тренера подставляем только в карточке для тренера (в личке).
    # в группе for_admin=False → показываются обычные имена из Telegram.
    # Подписи берём по всем платформам: на тренировку записываются и из
    # tg, и из vk, и с веб-страницы (у web-подписи — телефон участника).
    aliases = await svc.repo.aliases_map_all() if for_admin else {}

    # прогресс-бар заполнения мест
    filled = len(active)
    total = t.max_participants
    bar = _progress_bar(filled, total)

    lines = [f"🏸 <b>{_html.escape(t.title)}</b>", f"📅 {svc.format_local(t.start_at)}"]
    if t.location:
        lines.append(f"📍 {_html.escape(t.location)}")
    master_line = await _master_line(svc, t)
    if master_line:
        lines.append(_html.escape(master_line))
    h = t.duration_min / 60
    lines.append(f"⏱ {('%.1f' % h).rstrip('0').rstrip('.')} ч")
    if getattr(t, "price_minor", 0):
        lines.append(f"💰 {t.price_minor // 100}₽")
    lines.append(f"👥 {filled}/{total}  {bar}")

    if t.state == "draft":
        if t.publish_at:
            lines.append(f"📝 <i>Черновик — запись откроется {svc.format_local(t.publish_at)}</i>")
        else:
            lines.append("📝 <i>Черновик — запись не открыта</i>")

    if active:
        lines.append("\n<b>Записаны:</b>")
        lines += [f"  {i}. {_html.escape(_label(s, aliases))}"
                  for i, s in enumerate(active, 1)]
    if queue:
        lines.append("\n<b>⏳ Очередь:</b>")
        lines += [f"  {i}. {_html.escape(_label(s, aliases))}"
                  for i, s in enumerate(queue, 1)]
    if not active and not queue:
        lines.append("\n<i>Пока никто не записан — будь первым!</i>")
    return "\n".join(lines)


async def training_card_plain(svc: BookingService, t,
                              aliases: dict[tuple[str, int], str] | None = None,
                              ) -> str:
    """
    Карточка тренировки БЕЗ HTML — для VK (VK не поддерживает разметку).
    Показывает те же данные: дату, место, длительность, цену, счётчик,
    прогресс-бар, список записавшихся и очередь. Если передан aliases
    (для тренера) — имена участников заменяются на приватные подписи.
    """
    active = await svc.repo.get_signups(t.id, "active")
    queue = await svc.repo.get_signups(t.id, "queue")
    filled = len(active)
    total = t.max_participants
    bar = _progress_bar(filled, total)

    lines = [f"🏸 {t.title}", f"📅 {svc.format_local(t.start_at)}"]
    if t.location:
        lines.append(f"📍 {t.location}")
    master_line = await _master_line(svc, t)
    if master_line:
        lines.append(master_line)
    h = t.duration_min / 60
    lines.append(f"⏱ {('%.1f' % h).rstrip('0').rstrip('.')} ч")
    if getattr(t, "price_minor", 0):
        lines.append(f"💰 {t.price_minor // 100}₽")
    lines.append(f"👥 {filled}/{total}  {bar}")

    if active:
        lines.append("\nЗаписаны:")
        lines += [f"  {i}. {_label(s, aliases)}" for i, s in enumerate(active, 1)]
    if queue:
        lines.append("\n⏳ Очередь:")
        lines += [f"  {i}. {_label(s, aliases)}" for i, s in enumerate(queue, 1)]
    if not active and not queue:
        lines.append("\nПока никто не записан — будь первым!")
    return "\n".join(lines)


async def announce_card_plain(svc: BookingService, t) -> str:
    """
    Короткий анонс тренировки для поста на стене VK — БЕЗ счётчика мест,
    списка записанных и прогресс-бара (пост статичен и не обновляется).
    Только суть: название, дата, место, длительность, цена.
    """
    lines = [f"🏸 {t.title}", f"📅 {svc.format_local(t.start_at)}"]
    if t.location:
        lines.append(f"📍 {t.location}")
    h = t.duration_min / 60
    lines.append(f"⏱ {('%.1f' % h).rstrip('0').rstrip('.')} ч")
    if getattr(t, "price_minor", 0):
        lines.append(f"💰 {t.price_minor // 100}₽")
    return "\n".join(lines)


async def _master_line(svc: BookingService, t) -> str:
    """Строка «👤 Тренер: Имя» / «👤 Мастер: Имя» для карточки — слово
    берётся из вертикали клуба (app/core/verticals.py). Пустая строка,
    если у слота мастер/тренер не задан."""
    mid = getattr(t, "master_id", None)
    if not mid:
        return ""
    m = await svc.repo.get_master(mid)
    if not m:
        return ""
    from app.core.verticals import vcfg
    from app.models.entities import Tenant
    tenant = await svc.session.get(Tenant, svc.tenant_id)
    vc = vcfg(getattr(tenant, "vertical", None) if tenant else None)
    spec = f", {m.specialty}" if m.specialty else ""
    return f"👤 {vc['master_word_cap']}: {m.name}{spec}"


def _progress_bar(filled: int, total: int, width: int = 10) -> str:
    """Текстовый прогресс-бар заполнения мест: ▰▰▰▱▱▱▱▱▱▱"""
    if total <= 0:
        return ""
    ratio = min(1.0, filled / total)
    full = round(ratio * width)
    return "▰" * full + "▱" * (width - full)


def _label(s, aliases: dict[tuple[str, int], str] | None = None) -> str:
    """Имя участника с @username (если есть) и пометкой гостя.
    Если передан словарь подписей {(platform, user_id): alias} и для
    участника есть подпись — она заменяет имя.
    """
    if getattr(s, "is_guest", False):
        mark = "✅" if s.confirmed else "⏳ требует подтверждения"
        return f"{s.name} (гость, {mark})"
    name = s.name
    if aliases:
        alias = aliases.get((getattr(s, "platform", None),
                             getattr(s, "user_id", None)))
        if alias:
            name = alias
    uname = getattr(s, "username", None)
    suffix = f" @{uname}" if uname else ""
    return f"{name}{suffix}"


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


async def onboarding_text(svc) -> str | None:
    """Чеклист первых шагов для тренера нового (пустого) клуба."""
    trainings = await svc.repo.list_upcoming()
    schedules = await svc.repo.list_schedules()
    subs = await svc.repo.get_subscribers()
    done_t = bool(trainings)
    done_s = bool(schedules)
    done_p = len(subs) > 1
    if done_t and done_s and done_p:
        return None
    def m(x): return "✅" if x else "⬜"
    return ("🚀 Первые шаги:\n"
            f"{m(done_t)} Создайте тренировку — кнопка «➕»\n"
            f"{m(done_s)} Настройте «📆 Расписание» — тренировки будут "
            "создаваться сами\n"
            f"{m(done_p)} Пришлите участникам ссылку на бота или "
            "страницу записи\n\n"
            "Хотите посмотреть на живом примере? Отправьте слово «демо» — "
            "клуб наполнится образцами (только пока он пуст).")
