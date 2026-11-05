#!/bin/bash

# One-click launcher for NyoAnime Watch Together on Linux/macOS

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
.
venv/bin/activate

# Check if dependencies are installed
if [ ! -f "requirements.txt" ]; then
    echo "requirements.txt not found in project root"
    exit 1
fi

# Install dependencies if needed
if [ ! -f "venv/.dependencies-installed" ]; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
    touch venv/.dependencies-installed
fi

# Run the main application
exec "$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/main.py" "$@"
