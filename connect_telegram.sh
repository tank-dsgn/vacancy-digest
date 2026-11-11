#!/bin/bash
# Connects Telegram to the cloud digest: links the bot and stores the token in GitHub secrets.
# Usage: bash ~/Documents/CLAUDE/vacancy-digest/connect_telegram.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$HOME/Library/Application Support/vacancy-digest/telegram.json"
export GH_CONFIG_DIR="$HOME/Library/Application Support/gh"
GH="$HOME/.local/bin/gh"
REPO="tank-dsgn/vacancy-digest"

if [ ! -f "$CONF" ]; then
  python3 "$DIR/../job-search/вакансии/telegram_setup.py"
fi

python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"], end="")' "$CONF" | "$GH" secret set TELEGRAM_TOKEN --repo "$REPO"
python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["chat_id"], end="")' "$CONF" | "$GH" secret set TELEGRAM_CHAT_ID --repo "$REPO"
echo "Секреты сохранены в GitHub. Пробный запуск…"
"$GH" workflow run daily.yml --repo "$REPO"
echo "Через 3–5 минут в Telegram придут сводка и файл со списком. Дальше — каждый день около 9:00."
