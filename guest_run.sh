#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# Load .env file if it exists
if [ -f ".env" ]; then
    echo "[+] Loading environment variables from .env..."
    set -a
    source .env
    set +a
fi

if [ ! -d "venv" ]; then
    echo "[+] Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate

if ! python3 -c "import rich, supabase" &>/dev/null; then
    echo "[+] Installing requirements..."
    pip install -r requirements.txt
fi

ROOM_CODE="${1:-}"
shift || true

if [ -z "$ROOM_CODE" ]; then
    read -r -p "Enter Watch Together Room Code: " ROOM_CODE
fi

ROOM_CODE="$(printf '%s' "$ROOM_CODE" | tr -d '[:space:]')"
if [ -z "$ROOM_CODE" ]; then
    echo "A room code is required to join." >&2
    exit 1
fi

# A separate profile keeps the guest signed in as its own participant even
# when the host runs on the same machine.
export NYOANIME_WATCH_TOGETHER_NAME=Guest

exec python main.py \
    --session-dir "$HOME/.nyoanime/guest" \
    --join-room "$ROOM_CODE" \
    "$@"
