"""Per-provider stream time offsets for Watch Together.

A guest's offset is added to the host timeline before seeking, which is how a
dub with an extra studio intro stays aligned with a subbed reference. Values
are stored outside the repository at ``~/.nyoanime/database/stream_offsets.json``
and keyed by canonical provider id plus language mode, so a dub's offset never
leaks onto the subbed version of the same show.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from ..providers.language import normalize_language_mode

logger = logging.getLogger(__name__)

MAX_OFFSET_SECONDS = 120.0


def stream_offsets_path() -> Path:
    return Path.home() / ".nyoanime" / "database" / "stream_offsets.json"


def canonical_provider_id(provider_id: Any) -> str:
    text = str(provider_id or "").strip()
    if not text:
        return ""
    try:
        from ..providers.manager import ProviderManager

        return str(ProviderManager.get_canonical_id(text) or text)
    except Exception:
        return text


def offset_key(provider_id: Any, language_mode: Any) -> str:
    provider = canonical_provider_id(provider_id) or "unknown"
    return f"{provider}:{normalize_language_mode(language_mode)}"


def clamp_offset(seconds: Any) -> float:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    value = max(-MAX_OFFSET_SECONDS, min(MAX_OFFSET_SECONDS, value))
    return round(value, 2)


def _load_all(path: Optional[Path] = None) -> Dict[str, Any]:
    target = path or stream_offsets_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    data = raw.get("offsets")
    return data if isinstance(data, dict) else {}


def load_stream_offset(
    provider_id: Any,
    language_mode: Any,
    *,
    path: Optional[Path] = None,
) -> float:
    data = _load_all(path)
    return clamp_offset(data.get(offset_key(provider_id, language_mode), 0.0))


def save_stream_offset(
    provider_id: Any,
    language_mode: Any,
    seconds: Any,
    *,
    path: Optional[Path] = None,
) -> float:
    value = clamp_offset(seconds)
    target = path or stream_offsets_path()
    key = offset_key(provider_id, language_mode)
    data = _load_all(target)
    other_keys = {
        str(name): clamp_offset(raw)
        for name, raw in data.items()
        if str(name) != key and clamp_offset(raw) != 0.0
    }
    if value != 0.0:
        other_keys[key] = value
    payload = {"version": 1, "offsets": other_keys}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=".stream_offsets-",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(handle.name, target)
        except Exception:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
    except OSError as exc:
        # Losing a preference is annoying, not fatal; playback continues.
        logger.debug("Could not save stream offset: %s", exc)
    return value
