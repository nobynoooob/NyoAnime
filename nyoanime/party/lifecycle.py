"""Process-exit hooks that release rooms a host left behind.

A terminal that closes abruptly (SIGHUP/SIGTERM) never reaches the normal
``leave_room`` path, so ``create_watch_room`` later rejects the account with
``too_many_rooms``. This module keeps a registry of clients that currently host
a Supabase room and runs the existing Edge Function cleanup on exit.

``SIGINT`` is delegated to the previous handler after scheduling a live leave:
the UI already awaits ``leave_room`` while unwinding a KeyboardInterrupt, and
the ``atexit`` hook is the backstop. ``SIGTERM``/``SIGHUP`` run a bounded
blocking cleanup before the process goes away because the event loop is not
guaranteed to spin again.
"""
import asyncio
import atexit
import signal
import threading
from typing import Any, Dict, Optional

_HOSTS: set = set()
_INSTALLED = False
_PREVIOUS: Dict[int, Any] = {}
_LOCK = threading.Lock()

CLEANUP_TIMEOUT_SECONDS = 8.0


def mark_hosting(client: Any) -> None:
    """Track ``client`` until its membership is confirmed released."""
    with _LOCK:
        _HOSTS.add(client)
    _install()


def unmark_hosting(client: Any) -> None:
    with _LOCK:
        _HOSTS.discard(client)


def active_hosts() -> list:
    with _LOCK:
        return list(_HOSTS)


def run_exit_cleanup() -> int:
    """Leave every active hosted room for the persisted account, best-effort."""
    if not active_hosts():
        return 0
    try:
        from .cleanup import leave_active_hosted_rooms_blocking
    except Exception:
        return 0
    try:
        return int(leave_active_hosted_rooms_blocking(CLEANUP_TIMEOUT_SECONDS))
    except Exception:
        return 0


def _install() -> None:
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        _INSTALLED = True
    atexit.register(run_exit_cleanup)
    if threading.current_thread() is not threading.main_thread():
        return
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            _PREVIOUS[signum] = signal.getsignal(signum)
            signal.signal(signum, _handle_signal)
        except (ValueError, OSError, RuntimeError):
            _PREVIOUS.pop(signum, None)


def _handle_signal(signum: int, frame: Any) -> None:
    if signum == signal.SIGINT:
        _schedule_live_leave()
        _chain_previous(signum, frame)
        return

    # The main loop is usually blocked in this thread, so a live coroutine
    # cannot finish here. A private thread with its own loop can.
    run_exit_cleanup()
    _chain_previous(signum, frame)
    raise SystemExit(128 + signum)


def _schedule_live_leave() -> None:
    for client in active_hosts():
        loop = getattr(client, "_loop", None)
        if loop is None or loop.is_closed():
            continue
        try:
            loop.call_soon_threadsafe(
                lambda c=client: asyncio.ensure_future(c.leave_room())
            )
        except RuntimeError:
            pass


def _chain_previous(signum: int, frame: Any) -> None:
    previous: Optional[Any] = _PREVIOUS.get(signum)
    if callable(previous):
        previous(signum, frame)


# ---------------------------------------------------------------------------
# Test support
# ---------------------------------------------------------------------------
def reset() -> None:
    """Drop tracked clients without touching the installed process hooks."""
    with _LOCK:
        _HOSTS.clear()
