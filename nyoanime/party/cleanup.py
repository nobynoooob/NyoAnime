"""Safe cleanup for orphaned Watch Together rooms.

Only three operations are allowed here:

1. Read the persisted Supabase Auth session.
2. Read the caller's memberships and active hosted rooms through RLS.
3. Invoke the existing ``leave-room`` Edge Function for each room.

There is deliberately no SQL table mutation and no RLS bypass in this module.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, TextIO
import asyncio
import sys
import threading

from .session_storage import FileSessionStorage
from .supabase_config import resolve_supabase_config
from .transport import RealtimeTransportError, SupabaseTransport


class CleanupError(Exception):
    """Raised when the cleanup cannot safely start or read its inputs."""


@dataclass(frozen=True)
class HostedRoom:
    room_id: str
    created_at: Optional[str] = None
    expires_at: Optional[str] = None


@dataclass(frozen=True)
class LeaveResult:
    room_id: str
    status: str
    detail: str = ""


async def create_authenticated_client(
    session_path: Optional[Path] = None,
    supabase_url: Optional[str] = None,
    supabase_key: Optional[str] = None,
):
    """Create a Supabase client that reuses the existing persisted session.

    This never signs in anonymously: a missing/expired session is an error so a
    cleanup run can never operate as a different account.
    """
    config = resolve_supabase_config(supabase_url, supabase_key)
    if config is None:
        raise CleanupError(
            "Supabase is not configured. Set SUPABASE_PUBLISHABLE_KEY or bake "
            "the publishable key into the build."
        )

    from supabase import acreate_client
    from supabase.lib.client_options import AsyncClientOptions

    storage = FileSessionStorage(Path(session_path) if session_path else None)
    client = await acreate_client(
        config[0],
        config[1],
        AsyncClientOptions(
            storage=storage,
            auto_refresh_token=True,
            persist_session=True,
        ),
    )

    try:
        session = await client.auth.get_session()
    except Exception as exc:
        raise CleanupError(
            "Could not load the persisted Supabase session. Run NyoAnime once "
            "and make sure Watch Together authenticated successfully."
        ) from exc

    if session is None or not getattr(session, "access_token", None):
        raise CleanupError(
            "No authenticated Supabase session was found. Run NyoAnime once "
            "and make sure Watch Together authenticated successfully."
        )

    user = getattr(session, "user", None)
    user_id = getattr(user, "id", None)
    if not user_id:
        raise CleanupError("The persisted Supabase session has no user id.")

    # Realtime is not used for cleanup, but this keeps the client consistent
    # with WTRClient if future operations ever need private channels.
    await client.realtime.set_auth(session.access_token)
    return client, str(user_id)


async def authenticated_user_id(client: Any) -> str:
    session = await client.auth.get_session()
    user_id = getattr(getattr(session, "user", None), "id", None)
    if not user_id:
        raise CleanupError("The Supabase session has no user id.")
    return str(user_id)


async def find_active_hosted_rooms(client: Any, user_id: str) -> List[HostedRoom]:
    """Return active rooms hosted by ``user_id`` that the caller belongs to."""
    try:
        member_response = await (
            client.table("watch_room_members")
            .select("room_id,role,joined_at")
            .eq("user_id", user_id)
            .execute()
        )
        room_response = await (
            client.table("watch_rooms")
            .select("id,host_user_id,status,created_at,expires_at")
            .eq("host_user_id", user_id)
            .eq("status", "active")
            .execute()
        )
    except Exception as exc:
        raise CleanupError(
            "Failed to read room memberships through RLS. The persisted session "
            "may be expired."
        ) from exc

    membership_ids = {
        str(row.get("room_id"))
        for row in (member_response.data or [])
        if isinstance(row, dict) and row.get("room_id")
    }

    rooms: List[HostedRoom] = []
    for row in room_response.data or []:
        if not isinstance(row, dict):
            continue
        room_id = row.get("id")
        if not room_id or str(room_id) not in membership_ids:
            continue
        if str(row.get("host_user_id")) != str(user_id):
            continue
        if str(row.get("status") or "").lower() != "active":
            continue
        rooms.append(HostedRoom(
            room_id=str(room_id),
            created_at=row.get("created_at"),
            expires_at=row.get("expires_at"),
        ))

    return sorted(rooms, key=lambda room: (room.created_at or "", room.room_id))


async def leave_room(client: Any, user_id: str, room_id: str) -> LeaveResult:
    """Invoke the existing authenticated leave-room Edge Function."""
    transport = SupabaseTransport(client, user_id)
    try:
        await transport.leave_room_by_id_checked(room_id)
    except RealtimeTransportError as exc:
        return LeaveResult(room_id, "failed", f"{exc.code}: {exc.description}")
    except Exception as exc:
        # Do not echo arbitrary exception text: HTTP client errors can include
        # request metadata. The type is enough for an operator to investigate.
        return LeaveResult(room_id, "failed", type(exc).__name__)
    return LeaveResult(room_id, "left")


async def leave_rooms(
    client: Any,
    user_id: str,
    rooms: List[HostedRoom],
    dry_run: bool = True,
) -> List[LeaveResult]:
    if dry_run:
        return [LeaveResult(room.room_id, "would_leave") for room in rooms]

    results: List[LeaveResult] = []
    for room in rooms:
        results.append(await leave_room(client, user_id, room.room_id))
    return results


async def leave_active_hosted_rooms(
    session_path: Optional[Path] = None,
    client: Any = None,
    user_id: Optional[str] = None,
) -> int:
    """Leave every active room hosted by the persisted account.

    Returns the number of rooms confirmed left. Raises :class:`CleanupError`
    when the session or room list cannot be read.
    """
    if client is None:
        client, loaded_user_id = await create_authenticated_client(session_path)
        user_id = user_id or loaded_user_id
    elif not user_id:
        user_id = await authenticated_user_id(client)

    rooms = await find_active_hosted_rooms(client, user_id)
    if not rooms:
        return 0
    results = await leave_rooms(client, user_id, rooms, dry_run=False)
    return sum(1 for result in results if result.status == "left")


def leave_active_hosted_rooms_blocking(
    timeout: float = 8.0,
    session_path: Optional[Path] = None,
) -> int:
    """Synchronous wrapper for ``atexit``/signal handlers.

    Runs on a private thread so it can start its own event loop even when the
    caller is inside a running loop (a signal handler in the main thread).
    """
    outcome = {"left": 0}

    def runner() -> None:
        try:
            outcome["left"] = asyncio.run(
                leave_active_hosted_rooms(session_path=session_path)
            )
        except Exception:
            # Exit cleanup is best-effort; never mask the real exit path.
            outcome["left"] = 0

    thread = threading.Thread(
        target=runner,
        name="nyoanime-room-cleanup",
        daemon=True,
    )
    thread.start()
    thread.join(max(0.5, float(timeout)))
    return outcome["left"]


async def run_cleanup(
    session_path: Optional[Path] = None,
    confirm: bool = False,
    client: Any = None,
    user_id: Optional[str] = None,
    out: Optional[TextIO] = None,
) -> int:
    """Run a dry-run by default; only ``confirm=True`` mutates anything."""
    out = out or sys.stdout

    try:
        if client is None:
            client, loaded_user_id = await create_authenticated_client(session_path)
            user_id = user_id or loaded_user_id
        elif not user_id:
            user_id = await authenticated_user_id(client)

        rooms = await find_active_hosted_rooms(client, user_id)
    except CleanupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: cleanup failed ({type(exc).__name__})", file=sys.stderr)
        return 2

    if not rooms:
        print("No active rooms hosted by this account.", file=out)
        return 0

    print(
        f"Found {len(rooms)} active room(s) hosted by this account:",
        file=out,
    )
    for room in rooms:
        details = []
        if room.created_at:
            details.append(f"created={room.created_at}")
        if room.expires_at:
            details.append(f"expires={room.expires_at}")
        suffix = f" ({', '.join(details)})" if details else ""
        print(f"  - {room.room_id}{suffix}", file=out)

    if not confirm:
        print(
            f"Dry run: {len(rooms)} active room(s) would be left.",
            file=out,
        )
        print("No changes made. Re-run with --confirm to leave these rooms.", file=out)
        return 0

    print(
        f"Leaving {len(rooms)} active room(s) through the leave-room Edge Function...",
        file=out,
    )
    results = await leave_rooms(client, user_id, rooms, dry_run=False)

    failures = 0
    for result in results:
        if result.status == "left":
            print(f"  [left] {result.room_id}", file=out)
        else:
            failures += 1
            print(f"  [failed] {result.room_id}: {result.detail}", file=out)

    if failures:
        print(
            f"Cleanup incomplete: {len(results) - failures} left, {failures} failed.",
            file=out,
        )
        return 1

    print(f"Cleanup complete: {len(results)} room(s) left.", file=out)
    return 0
