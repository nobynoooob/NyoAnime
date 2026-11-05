#!/usr/bin/env bash
set -e

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

python3 main.py "$@"
