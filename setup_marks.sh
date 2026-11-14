#!/bin/bash
# Turns on the Telegram buttons: creates a random key that encrypts your marks (applied / skip / rejected),
# keeps a copy on this Mac and stores it as the STATE_KEY secret of the repository.
# Usage: bash ~/Documents/CLAUDE/vacancy-digest/setup_marks.sh
set -e
export GH_CONFIG_DIR="$HOME/Library/Application Support/gh"
GH="$HOME/.local/bin/gh"
REPO="tank-dsgn/vacancy-digest"
KEYFILE="$HOME/Library/Application Support/vacancy-digest/state_key"

mkdir -p "$(dirname "$KEYFILE")"
if [ -f "$KEYFILE" ]; then
  echo "Ключ уже есть на этом Mac, использую его."
else
  openssl rand -base64 32 > "$KEYFILE"
  chmod 600 "$KEYFILE"
fi
tr -d '\n' < "$KEYFILE" | "$GH" secret set STATE_KEY --repo "$REPO"
echo "Готово: ключ сохранён в секретах GitHub. Кнопки появятся в следующей сводке."
