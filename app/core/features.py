"""
Возможности по редакциям.

Lite (один тренер): запись, очередь, напоминания, посещаемость.
Pro (клуб): всё из Lite + статистика/графики/экспорт, оплаты, несколько
            групп и клубов, полная админка с ролями, white-label.

Единая точка истины: и боты, и API, и админка спрашивают features.X,
а не проверяют edition напрямую — так проще менять состав тарифов.
"""
from app.core.config import settings


class Features:
    # --- доступно всегда (Lite и Pro) ---
    signup = True
    queue = True
    reminders = True
    attendance = True

    # --- только Pro ---
    @property
    def payments(self) -> bool:
        return settings.is_pro

    @property
    def statistics(self) -> bool:      # графики, рейтинг, должники
        return settings.is_pro

    @property
    def exports(self) -> bool:         # Excel/PDF
        return settings.is_pro

    @property
    def groups(self) -> bool:          # группы внутри клуба
        return settings.is_pro

    @property
    def multi_tenant(self) -> bool:    # несколько клубов
        return settings.is_pro

    @property
    def web_admin(self) -> bool:       # HTML-админка с ролями
        return settings.is_pro

    @property
    def white_label(self) -> bool:
        return settings.is_pro

    @property
    def edition_name(self) -> str:
        return "Pro" if settings.is_pro else "Lite"


features = Features()
