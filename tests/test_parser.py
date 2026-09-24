import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sheet_parser import a1, free_slots, make_matcher, my_slot, parse_seminars

# Таблица со скриншота
ROWS = [
    ["семенар", "1 семинар", "1 семинар", "1 семинар", "", ""],
    ["1 вопрос", "Ефимова Ксения", "Тихомиров", "панфилов", "Смирнов Макар", ""],
    ["2 вопрос", "Лебедев Глеб", "Баукина Елизавета", "Сечной Андрей", "Смирнов Артём", ""],
    ["3 вопрос", "Антипин Степан", "Чистяков Николай", "Работько", "Суханов Егор", ""],
    [""] * 6,
    [""] * 6,
    ["семенар", "2 семинар", "2 семинар", "2 семинар", "", ""],
    ["1 вопрос", "макар", "Мишенев", "ккка", "андронов", ""],
    ["2 вопрос", "Глеб", "степа", "Айрих", "Чистяков", ""],
    ["3 вопрос", "егор суханов", "Артем смирнов", "гриб", "Сечной", ""],
    [""] * 6,
    [""] * 6,
    ["семинар", "3 семинар", "3 семинар", "3 семинар", "3 семинар", ""],
    ["1 вопрос", "андронов", "Артем смирнов", "сечной", "Буров", "ефимова"],
    ["2 вопрос", "Айрих", "гриб", "ккка", "глеб", "Панфилов"],
    ["Индивидуальное задание", "", "", "", "", ""],
]


def test_parses_three_seminars():
    sems = parse_seminars(ROWS)
    assert [s.number for s in sems] == [1, 2, 3]
    assert [len(s.slot_rows) for s in sems] == [3, 3, 2]
    # Колонка E без заголовка в 1 семинаре всё равно место, раз там кто-то записан
    assert sems[0].slot_cols == [1, 2, 3, 4]
    # «Индивидуальное задание» — не место для записи
    assert 15 not in sems[2].slot_rows


def test_full_table_has_no_free_slots():
    assert all(free_slots(ROWS, s) == [] for s in parse_seminars(ROWS))


def test_new_seminar_block_gets_free_slot():
    rows = ROWS + [
        [""] * 6,
        ["семинар", "4 семинар", "4 семинар", "4 семинар", "", ""],
        ["1 вопрос", "андронов", "", "", "", ""],
        ["2 вопрос", "", "", "", "", ""],
    ]
    sem = parse_seminars(rows)[-1]
    assert sem.number == 4
    first = free_slots(rows, sem)[0]
    assert a1(*first) == "C19"


def test_new_column_in_existing_seminar():
    rows = [r[:] for r in ROWS]
    rows[12][5] = "3 семинар"
    rows[14][5] = ""  # Панфилов выписался
    assert a1(*free_slots(rows, parse_seminars(rows)[2])[0]) == "F15"


def test_matcher_finds_me_but_not_similar_names():
    m = make_matcher("Иванов И.И.", ["иванов"])
    assert m("Иванов И.И.") and m("иванов") and m("ИВАНОВ Иван")
    assert not m("Иванова") and not m("")


def test_my_slot():
    sems = parse_seminars(ROWS)
    m = make_matcher("Буров", [])
    assert my_slot(ROWS, sems[0], m) is None
    assert a1(*my_slot(ROWS, sems[2], m)) == "E14"
