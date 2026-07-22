"""Минимальный конструктор VK-клавиатур без тяжёлого ``vkbottle``.

VK принимает клавиатуру как обычный JSON. Проекту нужны только текстовые и
callback-кнопки, поэтому загружать сотни сгенерированных моделей VK API ради
четырёх небольших структур не требуется.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class KeyboardButtonColor:
    PRIMARY = "primary"
    SECONDARY = "secondary"
    NEGATIVE = "negative"
    POSITIVE = "positive"


@dataclass(frozen=True)
class _Action:
    label: str
    payload: dict[str, Any] | None = None

    kind = "text"

    def as_dict(self) -> dict[str, Any]:
        action: dict[str, Any] = {"type": self.kind, "label": self.label}
        if self.payload is not None:
            # VK ожидает payload именно JSON-строкой внутри JSON клавиатуры.
            action["payload"] = json.dumps(
                self.payload, ensure_ascii=False, separators=(",", ":")
            )
        return action


class Text(_Action):
    kind = "text"


class Callback(_Action):
    kind = "callback"


class Keyboard:
    """Совместимый поднабор интерфейса ``vkbottle.Keyboard``."""

    def __init__(self, *, inline: bool = False, one_time: bool = False):
        self.inline = inline
        self.one_time = one_time
        self._rows: list[list[dict[str, Any]]] = [[]]

    def add(self, action: _Action, color: str | None = None) -> "Keyboard":
        if not self._rows:
            self._rows.append([])
        self._rows[-1].append({
            "action": action.as_dict(),
            "color": color or KeyboardButtonColor.SECONDARY,
        })
        return self

    def row(self) -> "Keyboard":
        if self._rows and self._rows[-1]:
            self._rows.append([])
        return self

    def as_dict(self) -> dict[str, Any]:
        rows = [row for row in self._rows if row]
        return {
            "one_time": self.one_time,
            "inline": self.inline,
            "buttons": rows,
        }

    def get_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))
