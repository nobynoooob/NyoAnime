"""
Developer-provided Supabase client configuration.

Only the project URL and the *publishable* (anon) key belong here. Service-role
keys, database passwords and other administrative secrets must never be shipped
with NyoAnime.

Ordinary users do not configure anything: a release build bakes in the project
URL and publishable key below, or the packager supplies both through the
environment. The WebSocket relay remains available as an explicit fallback.
"""
import os
from typing import Optional, Tuple

# Public project endpoint for the NyoAnime Watch Together backend.
DEFAULT_SUPABASE_URL = "https://mxacanxpjkqeudlwzhwr.supabase.co"

# Intentionally empty in the repository. Release builds/packagers inject the
# public publishable key; without it NyoAnime falls back to the WebSocket relay.
# Fallback for development/testing when no environment variables are provided.
DEFAULT_SUPABASE_PUBLISHABLE_KEY = "sb_publishable_3vCJNzwcIhmLI2QE75nNuA_uy5EG_Uh"

_TRUTHY = {"1", "true", "yes", "on"}
_PUBLISHABLE_PREFIX = "sb_publishable_"


class SupabaseConfigError(ConnectionError):
    """Raised when a Supabase key is present but cannot be a valid API key."""

    code = "invalid_supabase_config"


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def validate_publishable_key(key: str) -> Optional[str]:
    """Return a human-readable problem, or None when the key looks usable.

    Supabase currently issues ``sb_publishable_*`` keys; older projects still
    use an anon JWT. Anything else (notably a pasted shell command) would only
    fail later as a generic edge-function error, so reject it up front.
    """
    value = _clean(key)
    if not value:
        return "a Supabase publishable key is required"
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        return (
            "the Supabase publishable key must be a single token without spaces; "
            "paste the project's key, not a command"
        )

    if value.startswith(_PUBLISHABLE_PREFIX):
        if len(value) <= len(_PUBLISHABLE_PREFIX) + 4:
            return "the Supabase publishable key is too short"
        return None

    # Legacy anon/service keys are JWTs. Only the anon key belongs in the app.
    if value.startswith("eyJ") and value.count(".") == 2:
        return None

    return (
        "the Supabase publishable key is invalid; use the project's "
        "publishable (or legacy anon) key, not a command"
    )


def is_supabase_configured() -> bool:
    """Check if Supabase configuration is available (environment or .env)."""
    return (
        bool(os.environ.get("SUPABASE_URL"))
        or bool(os.environ.get("SUPABASE_PUBLISHABLE_KEY"))
        or bool(os.environ.get("SUPABASE_ANON_KEY"))
        or bool(_clean(os.environ.get("SUPABASE_URL"))) != DEFAULT_SUPABASE_URL
    )


def describe_config_issue(url: str, key: str) -> Optional[str]:
    """Validate a resolved (url, key) pair without ever echoing the key."""
    if not _clean(url).startswith(("https://", "http://")):
        return "the Supabase URL must start with https:// or http://"
    return validate_publishable_key(key)


def resolve_supabase_config(
    explicit_url: Optional[str] = None,
    explicit_key: Optional[str] = None,
    settings=None,
) -> Optional[Tuple[str, str]]:
    """Return (url, publishable_key) or None when Supabase is not configured.
    
    Falls back to live Supabase credentials when no local environment is provided,
    ensuring zero-configuration access to the production Watch Together backend.
    """
    url = _clean(explicit_url)
    key = _clean(explicit_key)

    if settings is not None:
        url = url or _clean(settings.get("supabase_url"))
        key = key or _clean(settings.get("supabase_publishable_key"))

    # Default to live Supabase project URL and publishable key when no env vars provided
    url = url or _clean(os.environ.get("SUPABASE_URL")) or DEFAULT_SUPABASE_URL
    key = (
        key
        or _clean(os.environ.get("SUPABASE_PUBLISHABLE_KEY"))
        or _clean(os.environ.get("SUPABASE_ANON_KEY"))
        or DEFAULT_SUPABASE_PUBLISHABLE_KEY
    )

    if not url or not key:
        return None
    if not url.startswith(("https://", "http://")):
        return None
    return url, key


def use_supabase_transport() -> bool:
    """True unless the WebSocket relay was explicitly requested.
    
    Defaults to Supabase Realtime Broadcast (the recommended production mode).
    The WebSocket relay is only used when explicitly configured via
    NYOANIME_WATCH_TOGETHER_TRANSPORT=websocket|ws|relay or USE_SUPABASE_REALM=0|false|no|off.
    
    For development without environment variables, this will default to Supabase
    Realtime using the project's fallback credentials. To use the local WebSocket
    relay, explicitly set NYOANIME_WATCH_TOGETHER_TRANSPORT=websocket.
    """
    mode = _clean(os.environ.get("NYOANIME_WATCH_TOGETHER_TRANSPORT")).lower()
    if mode in {"websocket", "ws", "relay"}:
        return False
    legacy = _clean(os.environ.get("USE_SUPABASE_REALM")).lower()
    if legacy in {"0", "false", "no", "off"}:
        return False
    return True
