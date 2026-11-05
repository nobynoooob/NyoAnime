"""
File-backed Supabase Auth session storage.

The anonymous session must survive process restarts so reconnecting clients keep
the same auth.users identity. Tokens are written atomically with 0600
permissions and never logged.

A second local instance can opt into an isolated session with
``NYOANIME_SESSION_DIR`` (directory) or ``NYOANIME_SUPABASE_SESSION_PATH``
(exact file). Both map to the same session file layout used by the client.
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from supabase_auth import AsyncSupportedStorage

DEFAULT_SESSION_FILENAME = "supabase_session.json"
SESSION_PATH_ENV = "NYOANIME_SUPABASE_SESSION_PATH"
SESSION_DIR_ENV = "NYOANIME_SESSION_DIR"


def default_session_path() -> Path:
    """Primary user session. Computed per call so HOME overrides work."""
    return Path.home() / ".nyoanime" / "database" / DEFAULT_SESSION_FILENAME


def resolve_session_path(explicit: Optional[Path] = None) -> Path:
    """Resolve the session file, newest override wins.

    Precedence: explicit argument > NYOANIME_SUPABASE_SESSION_PATH >
    NYOANIME_SESSION_DIR/supabase_session.json > default path.
    """
    if explicit is not None:
        return Path(explicit).expanduser()

    file_override = os.environ.get(SESSION_PATH_ENV, "").strip()
    if file_override:
        return Path(file_override).expanduser()

    dir_override = os.environ.get(SESSION_DIR_ENV, "").strip()
    if dir_override:
        return Path(dir_override).expanduser() / DEFAULT_SESSION_FILENAME

    return default_session_path()


def set_session_override(
    session_dir: Optional[Path] = None,
    session_path: Optional[Path] = None,
) -> Optional[Path]:
    """Apply CLI overrides through the environment (single resolution point).

    ``session_path`` wins when both are supplied. Returns the resolved file so
    callers can log or display it without re-implementing precedence.
    """
    if session_path:
        path = Path(session_path).expanduser()
        os.environ[SESSION_PATH_ENV] = str(path)
        return path
    if session_dir:
        path = Path(session_dir).expanduser() / DEFAULT_SESSION_FILENAME
        os.environ[SESSION_PATH_ENV] = str(path)
        return path
    return None


def session_path_from_environment() -> Optional[Path]:
    """Return the configured override, or None when the default is in use."""
    if os.environ.get(SESSION_PATH_ENV, "").strip():
        return resolve_session_path()
    if os.environ.get(SESSION_DIR_ENV, "").strip():
        return resolve_session_path()
    return None


class FileSessionStorage(AsyncSupportedStorage):
    def __init__(self, path: Optional[Path] = None):
        self.path = resolve_session_path(path)

    @staticmethod
    def _restrict_directory(path: Path) -> None:
        # Session files contain refresh tokens; keep the profile directory
        # private even when the caller created it with a permissive umask.
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    async def get_item(self, key: str) -> Optional[str]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        value = data.get(key) if isinstance(data, dict) else None
        return value if isinstance(value, str) else None

    async def set_item(self, key: str, value: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restrict_directory(self.path.parent)
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                data = {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        data[key] = value

        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".session-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass

    async def remove_item(self, key: str) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict) or key not in data:
            return
        del data[key]
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.chmod(self.path, 0o600)
        except OSError:
            pass
