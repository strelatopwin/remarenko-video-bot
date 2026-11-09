#!/bin/bash
set -euo pipefail

: "${BOT_TOKEN:?Set BOT_TOKEN in Railway Variables}"
: "${TELEGRAM_API_ID:?Set TELEGRAM_API_ID in Railway Variables}"
: "${TELEGRAM_API_HASH:?Set TELEGRAM_API_HASH in Railway Variables}"

telegram-bot-api --local --http-ip-address=127.0.0.1 --http-port=8081 \
  --dir="$TELEGRAM_CACHE_DIR" --temp-dir=/tmp/telegram-bot-api \
  --api-id="$TELEGRAM_API_ID" --api-hash="$TELEGRAM_API_HASH" &
api_pid=$!
/opt/venv/bin/python -u /app/bot.py &
bot_pid=$!

cleanup() {
  kill "$bot_pid" "$api_pid" 2>/dev/null || true
  wait "$bot_pid" "$api_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT
wait -n "$api_pid" "$bot_pid"
exit 1
