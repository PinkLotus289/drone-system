#!/usr/bin/env bash
# Запуск web-приложения на сервере без systemd (для ручного старта / отладки).
# Поднимает uvicorn за reverse-proxy (Caddy/nginx терминирует TLS снаружи).
set -euo pipefail
cd "$(dirname "$0")/.."

# venv/bin в PATH — иначе порождаемые мосты (`python -m ...`) возьмут системный python без mavsdk.
export PATH="$PWD/.venv/bin:$PATH"
export PYTHONPATH="$PWD/src"

# Подхватываем .env (секреты, COOKIE_SECURE и пр.), если он есть.
if [ -f .env ]; then set -a; . ./.env; set +a; fi

exec ./.venv/bin/uvicorn web_ui.main:app \
    --host 127.0.0.1 --port 8000 \
    --proxy-headers --forwarded-allow-ips='*'
