"""Бот, который ловит место в Google-таблице записи на семинары.

Каждую секунду перечитывает таблицу. Как только в нужном семинаре
(например, 4-м) есть свободное место и таблицу открыли на редактирование —
вписывает твоё имя (сначала пробует нужный вопрос, например 2-й),
проверяет, что запись осталась, и завершает работу. Ровно одна запись.

Запуск:
    python bot.py --check       # проверить доступ и показать, что бот видит
    python bot.py --dry-run     # следить, но ничего не писать
    python bot.py               # боевой режим
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sys
import time
from pathlib import Path

import gspread

from sheet_parser import (
    a1,
    cell,
    find_seminar,
    make_matcher,
    my_slot,
    ordered_free_slots,
    parse_seminars,
)

log = logging.getLogger("seminar-bot")

DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files/{id}"
EXPORT_URL = "https://docs.google.com/spreadsheets/d/{id}/export?format=csv&gid={gid}"


# ---------------------------------------------------------------- настройки

def load_config(path: str) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg.setdefault("worksheet", None)
    cfg.setdefault("aliases", [])
    cfg.setdefault("target_seminar", None)
    cfg.setdefault("preferred_questions", [])
    cfg.setdefault("allow_other_questions", True)
    cfg.setdefault("row_label_pattern", "вопрос")
    cfg.setdefault("poll_seconds", 1)
    cfg.setdefault("read_via", "csv")
    cfg.setdefault("auth", "oauth")
    cfg.setdefault("credentials_file", "credentials.json")
    cfg.setdefault("service_account_file", "service_account.json")
    if not cfg.get("spreadsheet") or not cfg.get("name"):
        sys.exit("В config.json нужно указать хотя бы 'spreadsheet' и 'name'.")
    return cfg


def open_worksheet(cfg: dict) -> tuple[gspread.Client, gspread.Worksheet]:
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
    ws = sh.worksheet(cfg["worksheet"]) if cfg["worksheet"] else sh.sheet1
    return gc, ws


# ---------------------------------------------------------------- чтение / запись

class Sheet:
    """Чтение таблицы и запись в неё с понятными ошибками."""

    def __init__(self, gc: gspread.Client, ws: gspread.Worksheet, read_via: str):
        self.gc = gc
        self.ws = ws
        self.read_via = read_via
        self.session = gc.http_client.session  # авторизованная сессия requests
        self.csv_retry_at = 0.0
        self.last_read_via = read_via

    def read(self) -> list[list[str]]:
        """Всё содержимое листа.

        По умолчанию через CSV-экспорт: он не расходует квоту Sheets API
        (60 чтений в минуту), поэтому можно опрашивать раз в секунду.
        Если экспорт не отдаётся — откатываемся на API.
        """
        if self.read_via == "csv" and time.monotonic() >= self.csv_retry_at:
            try:
                url = EXPORT_URL.format(id=self.ws.spreadsheet_id, gid=self.ws.id)
                resp = self.session.get(url, timeout=10)
                if resp.status_code == 200 and "text/csv" in resp.headers.get("Content-Type", ""):
                    self.last_read_via = "csv"
                    return list(csv.reader(io.StringIO(resp.content.decode("utf-8"))))
                log.warning("CSV-экспорт не отдался (HTTP %s), 30 с читаю через API", resp.status_code)
            except Exception as e:  # noqa: BLE001
                log.warning("CSV-экспорт упал (%s), 30 с читаю через API", e)
            self.csv_retry_at = time.monotonic() + 30
        self.last_read_via = "api"
        return self.ws.get_all_values()

    def read_cell(self, r: int, c: int) -> str:
        """Точное текущее значение клетки прямо из API (без кеша экспорта)."""
        return (self.ws.acell(a1(r, c)).value or "").strip()

    def write_cell(self, r: int, c: int, value: str) -> str:
        """'ok' | 'denied' (только просмотр) | 'error' (сеть/сбой — результат неизвестен)."""
        try:
            self.ws.update_acell(a1(r, c), value)
            return "ok"
        except gspread.exceptions.APIError as e:
            if e.response.status_code == 403:
                return "denied"
            log.warning("Ошибка записи: %s", e)
            return "error"
        except Exception as e:  # noqa: BLE001  (таймауты, обрывы сети)
            log.warning("Сбой при записи: %s", e)
            return "error"

    def can_edit(self) -> bool | None:
        try:
            resp = self.gc.http_client.request(
                "get",
                DRIVE_FILES_URL.format(id=self.ws.spreadsheet_id),
                params={"fields": "capabilities/canEdit", "supportsAllDrives": True},
            )
            return bool(resp.json()["capabilities"]["canEdit"])
        except Exception as e:  # noqa: BLE001
            log.debug("Не удалось узнать права: %s", e)
            return None


# ---------------------------------------------------------------- логика

class Hunter:
    def __init__(self, sheet: Sheet, cfg: dict, dry_run: bool):
        self.sheet = sheet
        self.cfg = cfg
        self.dry_run = dry_run
        self.matches = make_matcher(cfg["name"], cfg["aliases"])
        self.target = cfg["target_seminar"]
        self.known_keys: set[str] | None = None  # для режима «первый новый семинар»
        self._last_status = None
        self.denied = False  # последняя попытка записи упёрлась в «только просмотр»
        self.denied_ticks = 0

    def status(self, text: str, level=logging.INFO) -> None:
        """Пишет в лог только когда статус меняется — чтобы не спамить раз в секунду."""
        if text != self._last_status:
            log.log(level, text)
            self._last_status = text

    def pick_seminar(self, rows):
        seminars = parse_seminars(rows, self.cfg["row_label_pattern"])
        if self.target is not None:
            return find_seminar(seminars, int(self.target))
        # Цель не задана: ловим первый семинар, которого не было при запуске.
        if self.known_keys is None:
            self.known_keys = {s.key for s in seminars}
            log.info("Семинары при запуске: %s. Жду новый.", ", ".join(sorted(self.known_keys)) or "—")
            return None
        for s in seminars:
            if s.key not in self.known_keys:
                self.target = s.number if s.number is not None else None
                return s
        return None

    def confirm(self, r: int, c: int) -> bool | None:
        """Проверяет через API, что в клетке моё имя. None — не удалось проверить."""
        for attempt in range(5):
            try:
                return self.matches(self.sheet.read_cell(r, c))
            except Exception as e:  # noqa: BLE001
                log.warning("Не могу проверить %s (%s), повтор…", a1(r, c), e)
                time.sleep(1 + attempt)
        return None

    def tick(self) -> bool:
        """Одна проверка. True — я записан, можно завершаться."""
        rows = self.sheet.read()
        sem = self.pick_seminar(rows)
        if sem is None:
            want = f"{self.target} семинар" if self.target is not None else "новый семинар"
            self.status(f"⏳ Жду, пока появится {want}…")
            return False

        mine = my_slot(rows, sem, self.matches)
        if mine:
            log.info("✅ Ты уже записан: %s, клетка %s (%s) — «%s»",
                     sem.title, a1(*mine), cell(rows, mine[0], 0), cell(rows, *mine))
            return True

        slots = ordered_free_slots(
            rows, sem, self.cfg["preferred_questions"], self.cfg["allow_other_questions"]
        )
        if not slots:
            self.status(f"⏳ {sem.title}: свободных мест нет, жду…")
            return False

        r, c = slots[0]
        where = f"{a1(r, c)} ({cell(rows, r, 0)})"
        if self.dry_run:
            self.status(f"🧪 {sem.title}: записал бы в {where}")
            return False

        # Пока таблица только для просмотра, не тратим квоту Sheets API на
        # попытки записи: спрашиваем права у Drive API (там лимиты в сотни раз
        # больше). Раз в 5 проверок всё равно пробуем записать — на всякий случай.
        if self.denied:
            self.denied_ticks += 1
            if not self.sheet.can_edit() and self.denied_ticks % 5:
                return False

        # Клетку могли занять после того, как мы прочитали таблицу —
        # чужую запись не перетираем.
        try:
            if self.sheet.read_cell(r, c):
                log.info("%s только что заняли, ищу другое место", a1(r, c))
                return False
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог перепроверить %s (%s), пробую записать", a1(r, c), e)

        result = self.sheet.write_cell(r, c, self.cfg["name"])
        self.denied = result == "denied"
        if self.denied:
            self.status(f"🔒 {sem.title}: место {where} есть, но таблица пока только для просмотра. Долблю каждую секунду…")
            return False

        # И при 'ok', и при сбое сети запись могла пройти — проверяем, чтобы не записаться дважды.
        ok = self.confirm(r, c)
        if ok:
            log.info("🎉 ЗАПИСАЛ: %s, клетка %s — «%s»", sem.title, where, self.cfg["name"])
            return True
        if ok is None:
            # Не знаем, прошла ли запись. Второй раз не пишем — лучше остановиться.
            log.error("Не удалось подтвердить запись в %s. Проверь таблицу руками!", where)
            return True
        log.warning("Запись в %s не удержалась (её перезаписали), пробую другое место", where)
        return False


def run(hunter: Hunter, poll: float, once: bool) -> None:
    errors = 0
    while True:
        try:
            if hunter.tick():
                return
            errors = 0
        except gspread.exceptions.APIError as e:
            errors += 1
            wait = 2 if e.response.status_code == 429 else min(2 ** errors, 30)
            log.warning("Ошибка Google API (%s), пауза %s с", e, wait)
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001  (сеть, таймауты — не падаем)
            errors += 1
            wait = min(2 ** errors, 30)
            log.warning("Сбой (%s: %s), пауза %s с", type(e).__name__, e, wait)
            time.sleep(wait)
        if once:
            return
        time.sleep(poll)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="config.json")
    p.add_argument("--check", action="store_true", help="проверить доступ, показать таблицу глазами бота и выйти")
    p.add_argument("--dry-run", action="store_true", help="следить, но ничего не записывать")
    p.add_argument("--once", action="store_true", help="одна проверка и выход")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config(args.config)
    gc, ws = open_worksheet(cfg)
    sheet = Sheet(gc, ws, cfg["read_via"])

    target = f"{cfg['target_seminar']} семинар" if cfg["target_seminar"] is not None else "первый новый семинар"
    prefer = ", ".join(map(str, cfg["preferred_questions"])) or "любой"
    log.info("Таблица «%s» / лист «%s»", ws.spreadsheet.title, ws.title)
    log.info("Цель: %s, вопрос: %s, пишу «%s»", target, prefer, cfg["name"])
    edit = sheet.can_edit()
    log.info("Права сейчас: %s", {True: "редактирование ✅", False: "только просмотр 🔒", None: "не удалось узнать"}[edit])

    if args.check:
        rows = sheet.read()
        log.info("Читаю через: %s", sheet.last_read_via)
        for s in parse_seminars(rows, cfg["row_label_pattern"]):
            free = ordered_free_slots(rows, s, cfg["preferred_questions"])
            log.info("  %s: вопросов %d, мест в строке %d, свободно %d%s",
                     s.title, len(s.slot_rows), len(s.slot_cols), len(free),
                     f" (первое подходящее: {a1(*free[0])})" if free else "")
        return

    run(Hunter(sheet, cfg, args.dry_run), cfg["poll_seconds"], args.once)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
