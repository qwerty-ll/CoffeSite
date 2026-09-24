"""Бот, который следит за Google-таблицей записи на семинары и
записывает тебя в первое свободное место каждого семинара.

Запуск:
    python bot.py                 # следить за таблицей постоянно
    python bot.py --once          # одна проверка и выход
    python bot.py --dry-run       # ничего не писать, только показать, что бы сделал
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import gspread

from sheet_parser import a1, cell, free_slots, make_matcher, my_slot, parse_seminars

log = logging.getLogger("seminar-bot")


def load_config(path: str) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg.setdefault("worksheet", None)
    cfg.setdefault("aliases", [])
    cfg.setdefault("row_label_pattern", "вопрос")
    cfg.setdefault("only_new_seminars", False)
    cfg.setdefault("skip_seminars", [])
    cfg.setdefault("poll_seconds", 10)
    cfg.setdefault("auth", "oauth")
    cfg.setdefault("credentials_file", "credentials.json")
    cfg.setdefault("service_account_file", "service_account.json")
    if not cfg.get("spreadsheet") or not cfg.get("name"):
        sys.exit("В config.json нужно указать хотя бы 'spreadsheet' и 'name'.")
    return cfg


def open_worksheet(cfg: dict) -> gspread.Worksheet:
    if cfg["auth"] == "service_account":
        gc = gspread.service_account(filename=cfg["service_account_file"])
    else:
        # Вход под своим Google-аккаунтом: при первом запуске откроется браузер,
        # токен сохранится в authorized_user.json рядом с ботом.
        gc = gspread.oauth(
            credentials_filename=cfg["credentials_file"],
            authorized_user_filename="authorized_user.json",
        )
    ref = cfg["spreadsheet"]
    sh = gc.open_by_url(ref) if ref.startswith("http") else gc.open_by_key(ref)
    return sh.worksheet(cfg["worksheet"]) if cfg["worksheet"] else sh.sheet1


def check_once(ws: gspread.Worksheet, cfg: dict, ignored: set[str], dry_run: bool) -> None:
    rows = ws.get_all_values()
    matches = make_matcher(cfg["name"], cfg["aliases"])
    skip = {int(s) for s in cfg["skip_seminars"]}

    for sem in parse_seminars(rows, cfg["row_label_pattern"]):
        if sem.key in ignored or (sem.number is not None and sem.number in skip):
            continue

        mine = my_slot(rows, sem, matches)
        if mine:
            log.debug("%s: уже записан в %s", sem.title, a1(*mine))
            continue

        slots = free_slots(rows, sem)
        if not slots:
            log.debug("%s: свободных мест нет", sem.title)
            continue

        r, c = slots[0]
        where = f"{a1(r, c)} ({cell(rows, r, 0)})"
        if dry_run:
            log.info("%s: записал бы в %s", sem.title, where)
            continue

        # Перед записью ещё раз проверяем клетку — её могли занять за время опроса.
        if ws.acell(a1(r, c)).value:
            log.info("%s: %s только что заняли, попробую на следующем круге", sem.title, a1(r, c))
            continue

        ws.update_acell(a1(r, c), cfg["name"])
        time.sleep(1.5)
        if ws.acell(a1(r, c)).value == cfg["name"]:
            log.info("%s: ✅ записал в %s", sem.title, where)
        else:
            log.warning("%s: запись в %s перезатёрли, попробую ещё раз", sem.title, a1(r, c))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="config.json")
    p.add_argument("--once", action="store_true", help="одна проверка и выход")
    p.add_argument("--dry-run", action="store_true", help="ничего не записывать")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config(args.config)
    ws = open_worksheet(cfg)
    log.info("Слежу за «%s» / «%s», пишу как «%s»", ws.spreadsheet.title, ws.title, cfg["name"])

    ignored: set[str] = set()
    if cfg["only_new_seminars"]:
        rows = ws.get_all_values()
        ignored = {s.key for s in parse_seminars(rows, cfg["row_label_pattern"])}
        log.info("Уже существующие семинары пропускаю: %s", ", ".join(sorted(ignored)) or "—")

    delay = cfg["poll_seconds"]
    while True:
        try:
            check_once(ws, cfg, ignored, args.dry_run)
            delay = cfg["poll_seconds"]
        except gspread.exceptions.APIError as e:
            delay = min(delay * 2, 300)
            log.warning("Ошибка Google API (%s), повтор через %s с", e, delay)
        if args.once:
            break
        time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
