"""User-level Supabase configuration stored outside the repository.

The file lives at ``~/.nyoanime/.env``. It is intentionally *not* a general
dotenv loader: only ``SUPABASE_*``, ``NYOANIME_*`` and the legacy
``USE_SUPABASE_REALM`` keys are accepted, so a malformed file cannot override
``PATH`` or other process-critical variables.

Existing non-empty environment variables always win. Values are never logged.
"""
import getpass
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

ENV_FILENAME = ".env"
SUPABASE_URL_KEY = "SUPABASE_URL"
SUPABASE_PUBLISHABLE_KEY = "SUPABASE_PUBLISHABLE_KEY"
LEGACY_SUPABASE_ANON_KEY = "SUPABASE_ANON_KEY"

_ALLOWED_PREFIXES = ("SUPABASE_", "NYOANIME_")
_ALLOWED_EXACT = frozenset({"USE_SUPABASE_REALM"})
_BLOCKED_KEYS = frozenset(
    {
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_DB_PASSWORD",
    }
)
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def local_config_path() -> Path:
    """Return the user-level config path at call time (HOME overrides work)."""
    return Path.home() / ".nyoanime" / ENV_FILENAME


def _allowed(key: str) -> bool:
    if key in _BLOCKED_KEYS:
        return False
    return key in _ALLOWED_EXACT or key.startswith(_ALLOWED_PREFIXES)


def _parse_line(line: str) -> Optional[Tuple[str, str]]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    if "=" not in stripped:
        return None

    key, raw = stripped.split("=", 1)
    key = key.strip()
    if not _KEY_RE.match(key):
        return None

    value = raw.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.rfind(quote)
        if end <= 0:
            return None
        value = value[1:end]
        if quote == '"':
            value = (
                value.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
    else:
        # dotenv-style inline comments require whitespace before '#'.
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return key, value


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse an env file, returning the last value for each key."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, OSError, UnicodeError):
        return {}

    values: Dict[str, str] = {}
    for line in text.splitlines():
        parsed = _parse_line(line)
        if parsed is not None:
            key, value = parsed
            if _allowed(key):
                values[key] = value
    return values


def load_local_env(path: Optional[Path] = None) -> Optional[Path]:
    """Load the user config into ``os.environ`` without overriding exports."""
    target = Path(path).expanduser() if path is not None else local_config_path()
    values = parse_env_file(target)
    for key, value in values.items():
        current = os.environ.get(key)
        # A blank export is treated as unset so the file can supply a value.
        if current is None or not current.strip():
            os.environ[key] = value
    return target if target.exists() else None


def _format_value(value: str) -> str:
    if not value:
        return '""'
    if any(ch in value for ch in (" ", "\t", "\n", '"', "'", "#")):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    return value


def write_local_env(values: Dict[str, str], path: Optional[Path] = None) -> Path:
    """Merge values into the user config with private permissions."""
    target = Path(path).expanduser() if path is not None else local_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent != Path("."):
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass

    try:
        existing = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        existing = ""

    written = set()
    lines = []
    for line in existing.splitlines():
        parsed = _parse_line(line)
        key = parsed[0] if parsed is not None else None
        if key in values:
            if key in written:
                continue
            lines.append(f"{key}={_format_value(str(values[key]))}")
            written.add(key)
        else:
            lines.append(line)

    for key, value in values.items():
        if key not in written:
            lines.append(f"{key}={_format_value(str(value))}")
            written.add(key)

    text = "\n".join(lines).rstrip() + "\n"
    fd, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".env-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
        os.chmod(target, 0o600)
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass
    return target


def save_supabase_config(
    url: str,
    key: str,
    path: Optional[Path] = None,
) -> Path:
    """Persist the public Supabase endpoint and publishable key."""
    from .supabase_config import validate_publishable_key

    clean_url = (url or "").strip()
    clean_key = (key or "").strip()
    if not clean_url.startswith(("https://", "http://")):
        raise ValueError("Supabase URL must start with https:// or http://")
    issue = validate_publishable_key(clean_key)
    if issue:
        raise ValueError(issue[0].upper() + issue[1:])
    return write_local_env(
        {
            SUPABASE_URL_KEY: clean_url,
            SUPABASE_PUBLISHABLE_KEY: clean_key,
        },
        path=path,
    )


def run_supabase_setup(
    path: Optional[Path] = None,
    prompt: Callable[[str], str] = input,
    secret_prompt: Callable[[str], str] = getpass.getpass,
    default_url: Optional[str] = None,
) -> Path:
    """Interactive one-time setup. The key is never echoed or printed."""
    target = Path(path).expanduser() if path is not None else local_config_path()
    existing = parse_env_file(target)

    if default_url is None:
        from .supabase_config import DEFAULT_SUPABASE_URL

        default_url = DEFAULT_SUPABASE_URL

    current_url = (
        os.environ.get(SUPABASE_URL_KEY)
        or existing.get(SUPABASE_URL_KEY)
        or default_url
        or ""
    ).strip()
    entered_url = prompt(f"Supabase project URL [{current_url}]: ").strip()
    url = entered_url or current_url

    entered_key = secret_prompt("Supabase publishable key (input hidden): ").strip()
    if not entered_key:
        entered_key = (
            os.environ.get(SUPABASE_PUBLISHABLE_KEY)
            or existing.get(SUPABASE_PUBLISHABLE_KEY)
            or existing.get(LEGACY_SUPABASE_ANON_KEY)
            or ""
        ).strip()
    if not entered_key:
        raise ValueError("A Supabase publishable key is required")

    return save_supabase_config(url, entered_key, path=target)
