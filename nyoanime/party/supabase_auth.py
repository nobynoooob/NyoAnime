"""Anonymous Supabase session recovery.

Supabase persists anonymous sessions on disk. Those sessions outlive access
tokens, and a refresh token can become invalid after a project key change or a
long offline period. Room creation must recover from that state instead of
surfacing a generic "couldn't open the room" error.
"""
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_EXPIRY_MARGIN_SECONDS = 60


def _access_token(session: Any) -> str:
    return str(getattr(session, "access_token", "") or "")


def _expiring(session: Any) -> bool:
    expires_at = getattr(session, "expires_at", None)
    if not expires_at:
        return False
    try:
        return float(expires_at) <= time.time() + _EXPIRY_MARGIN_SECONDS
    except (TypeError, ValueError):
        return False


async def _discard_local_session(client: Any) -> None:
    """Forget the persisted session without depending on a live access token."""
    auth = getattr(client, "auth", None)
    if auth is None:
        return
    try:
        await auth.sign_out({"scope": "local"})
    except TypeError:
        # Older/fake auth clients may not accept the options argument.
        try:
            await auth.sign_out()
        except Exception:
            pass
    except Exception as exc:
        logger.warning(
            "Could not clear stale Supabase session (%s); continuing",
            type(exc).__name__,
        )


async def ensure_authenticated_session(
    client: Any, *, force_new: bool = False, allow_new_identity: bool = True
) -> Any:
    """Return a live session, re-authenticating anonymously when necessary.

    ``allow_new_identity=False`` is for active rooms: signing in anonymously
    again would mint a different user id and silently orphan the membership.
    """
    auth = getattr(client, "auth", None)
    if auth is None:
        raise ConnectionError("Supabase auth client is not available")

    session: Optional[Any] = None
    if not force_new:
        try:
            session = await auth.get_session()
        except Exception as exc:
            logger.warning(
                "Stored Supabase session could not be refreshed (%s); "
                "starting a new anonymous session",
                type(exc).__name__,
            )
            session = None

        if session is not None and _access_token(session):
            if not _expiring(session):
                return session
            # get_session() normally refreshes at this point; make the retry
            # explicit so a session that slipped through is still recovered.
            refresh_token = getattr(session, "refresh_token", None)
            if refresh_token:
                try:
                    response = await auth.refresh_session(refresh_token)
                    refreshed = getattr(response, "session", None)
                    if refreshed is not None and _access_token(refreshed):
                        return refreshed
                except Exception as exc:
                    logger.warning(
                        "Supabase token refresh failed (%s); "
                        "starting a new anonymous session",
                        type(exc).__name__,
                    )
            if not allow_new_identity:
                raise ConnectionError(
                    "Supabase session expired and could not be refreshed; "
                    "refusing to change identity while in a room"
                )

    if not allow_new_identity:
        raise ConnectionError(
            "Supabase session is unavailable; refusing to change identity "
            "while in a room"
        )

    await _discard_local_session(client)

    response = await auth.sign_in_anonymously()
    session = getattr(response, "session", None)
    if session is None or not _access_token(session):
        try:
            session = await auth.get_session()
        except Exception:
            session = None
    if session is None or not _access_token(session):
        raise ConnectionError("Supabase anonymous authentication did not return a session")
    return session
