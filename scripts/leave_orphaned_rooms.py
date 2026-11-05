#!/usr/bin/env python3
"""Leave orphaned Watch Together rooms hosted by the current account.

Read-only by default. Nothing is changed unless ``--confirm`` is supplied.

Usage:
    python scripts/leave_orphaned_rooms.py
    python scripts/leave_orphaned_rooms.py --dry-run
    python scripts/leave_orphaned_rooms.py --confirm
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nyoanime.party.cleanup import run_cleanup  # noqa: E402
from nyoanime.party.local_config import load_local_env  # noqa: E402


def main() -> int:
    load_local_env()
    parser = argparse.ArgumentParser(
        description=(
            "Safely leave Watch Together rooms hosted by the persisted "
            "Supabase account. Dry-run unless --confirm is given."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="List active hosted rooms without changing anything (default).",
    )
    mode.add_argument(
        "--confirm",
        action="store_true",
        help="Actually invoke leave-room for each active hosted room.",
    )
    parser.add_argument(
        "--session-path",
        type=Path,
        default=None,
        help=(
            "Override the persisted Supabase session path "
            "(default: ~/.nyoanime/database/supabase_session.json)."
        ),
    )
    args = parser.parse_args()

    return asyncio.run(run_cleanup(
        session_path=args.session_path,
        confirm=args.confirm,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
