"""Разбор таблицы записи на семинары.

Ожидаемый формат листа (как в таблице «Философия»):

    семенар  | 1 семинар | 1 семинар | 1 семинар |
    1 вопрос | Ефимова   | Тихомиров | панфилов  | Смирнов Макар
    2 вопрос | Лебедев   | Баукина   |           |
    3 вопрос | Антипин   |           | Работько  |

Строка, у которой в колонке A стоит «семинар»/«семенар», начинает блок.
Строки блока, у которых в колонке A есть подпись, подходящая под
``row_label_pattern`` (по умолчанию «вопрос»), — строки для записи.
Колонка блока считается местом для записи, если у неё заполнен заголовок
или в ней уже кто-то записан. Пустая клетка на пересечении такой строки и
такой колонки — свободное место.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADER_RE = re.compile(r"^\s*се[мн]\w*нар", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d+")


@dataclass
class Seminar:
    header_row: int  # индекс строки заголовка (с нуля)
    number: int | None  # номер семинара из заголовков, если есть
    slot_rows: list[int] = field(default_factory=list)
    slot_cols: list[int] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"seminar-{self.number}" if self.number is not None else f"row-{self.header_row + 1}"

    @property
    def title(self) -> str:
        if self.number is not None:
            return f"{self.number} семинар (строка {self.header_row + 1})"
        return f"семинар в строке {self.header_row + 1}"


def cell(rows: list[list[str]], r: int, c: int) -> str:
    if r < len(rows) and c < len(rows[r]):
        return (rows[r][c] or "").strip()
    return ""


def parse_seminars(rows: list[list[str]], row_label_pattern: str = "вопрос") -> list[Seminar]:
    label_re = re.compile(row_label_pattern, re.IGNORECASE)
    header_idx = [i for i, row in enumerate(rows) if HEADER_RE.match(cell(rows, i, 0))]
    width = max((len(r) for r in rows), default=0)

    seminars = []
    for n, h in enumerate(header_idx):
        end = header_idx[n + 1] if n + 1 < len(header_idx) else len(rows)
        slot_rows = [r for r in range(h + 1, end) if label_re.search(cell(rows, r, 0))]

        slot_cols = [
            c
            for c in range(1, width)
            if cell(rows, h, c) or any(cell(rows, r, c) for r in slot_rows)
        ]

        number = None
        for c in slot_cols:
            m = NUMBER_RE.search(cell(rows, h, c))
            if m:
                number = int(m.group())
                break

        seminars.append(Seminar(h, number, slot_rows, slot_cols))
    return seminars


def free_slots(rows: list[list[str]], seminar: Seminar) -> list[tuple[int, int]]:
    """Свободные клетки (строка, колонка) по порядку: 1 вопрос слева направо, потом 2 и т.д."""
    return [(r, c) for r in seminar.slot_rows for c in seminar.slot_cols if not cell(rows, r, c)]


def make_matcher(name: str, aliases: list[str]):
    """Функция, проверяющая, что в клетке записан именно я."""
    patterns = [re.compile(r"(?<!\w)" + re.escape(a.strip()) + r"(?!\w)", re.IGNORECASE)
                for a in [name, *aliases] if a.strip()]

    def matches(text: str) -> bool:
        return any(p.search(text) for p in patterns)

    return matches


def my_slot(rows: list[list[str]], seminar: Seminar, matches) -> tuple[int, int] | None:
    for r in seminar.slot_rows:
        for c in seminar.slot_cols:
            if matches(cell(rows, r, c)):
                return r, c
    return None


def a1(r: int, c: int) -> str:
    """(0, 0) -> 'A1'."""
    letters = ""
    c += 1
    while c:
        c, rem = divmod(c - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{r + 1}"
