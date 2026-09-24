#!/bin/bash
# Запуск бота на macOS: ставит зависимости и не даёт Маку уснуть, пока бот работает.
set -e
cd "$(dirname "$0")"
if [ ! -f credentials.json ]; then
  echo "Нет credentials.json — скачай его из Google Cloud Console (см. README) и положи в эту папку."
  exit 1
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
exec caffeinate -i .venv/bin/python bot.py "$@"
