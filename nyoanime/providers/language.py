"""
Language-mode resolution for NyoAnime.

Three user-facing modes are supported:
    ar_sub  -> Arabic providers (existing workflow)
    en_sub  -> HiAnime subbed source
    en_dub  -> HiAnime dubbed source

The mode is authoritative.  A provider can only be selected when its
``language_mode`` matches, which prevents accidentally playing an Arabic
episode while English Dub is selected (and vice versa).
"""

from typing import Dict, List, Optional

DEFAULT_LANGUAGE_MODE = "ar_sub"

LANGUAGE_MODES: Dict[str, Dict[str, str]] = {
    "ar_sub": {
        "label": "Arabic Sub",
        "badge": "AR SUB",
        "language": "ar",
        "audio_mode": "sub",
        "default_provider": "anime3rb",
    },
    "en_sub": {
        "label": "English Sub",
        "badge": "EN SUB",
        "language": "en",
        "audio_mode": "sub",
        "default_provider": "hianime",
    },
    "en_dub": {
        "label": "English Dub",
        "badge": "EN DUB",
        "language": "en",
        "audio_mode": "dub",
        "default_provider": "hianime_dub",
    },
}

MODE_CHOICES: List[str] = list(LANGUAGE_MODES)

_ALIASES = {
    "ar": "ar_sub",
    "arabic": "ar_sub",
    "arabic_sub": "ar_sub",
    "ar-sub": "ar_sub",
    "sub_ar": "ar_sub",
    "en": "en_sub",
    "english": "en_sub",
    "english_sub": "en_sub",
    "en-sub": "en_sub",
    "sub": "en_sub",
    "sub_en": "en_sub",
    "en_dub": "en_dub",
    "english_dub": "en_dub",
    "en-dub": "en_dub",
    "dub": "en_dub",
    "dub_en": "en_dub",
}


def normalize_language_mode(value: Optional[str]) -> str:
    """Return a canonical language mode, defaulting to Arabic sub."""
    key = str(value or "").strip().lower()
    key = key.replace("-", "_").replace(" ", "_")
    key = "_".join(part for part in key.split("_") if part)
    if key in LANGUAGE_MODES:
        return key
    return _ALIASES.get(key, DEFAULT_LANGUAGE_MODE)


def language_mode_label(value: Optional[str]) -> str:
    return LANGUAGE_MODES[normalize_language_mode(value)]["label"]


def language_badge(value: Optional[str]) -> str:
    return LANGUAGE_MODES[normalize_language_mode(value)]["badge"]


def mode_audio_lang(value: Optional[str]) -> str:
    mode = normalize_language_mode(value)
    return "jpn,ja" if mode == "en_sub" else ("eng,en" if mode == "en_dub" else "ara,ar")


def provider_language_mode(provider_id: Optional[str]) -> str:
    """Resolve the language mode advertised by a provider id (aliases included)."""
    if not provider_id:
        return DEFAULT_LANGUAGE_MODE
    from .manager import ProviderManager

    provider_cls = ProviderManager.get_provider_class(provider_id)
    if provider_cls is None:
        return DEFAULT_LANGUAGE_MODE
    return normalize_language_mode(getattr(provider_cls, "language_mode", "") or DEFAULT_LANGUAGE_MODE)


def provider_supports_mode(provider_id: Optional[str], mode: Optional[str]) -> bool:
    if not provider_id:
        return False
    return provider_language_mode(provider_id) == normalize_language_mode(mode)


def providers_for_mode(mode: Optional[str]) -> List[str]:
    """Canonical provider ids compatible with a language mode, in registry order."""
    target = normalize_language_mode(mode)
    from .manager import ProviderManager

    choices = ProviderManager.get_provider_choices()
    matches = [pid for pid in choices if provider_supports_mode(pid, target)]
    default_pid = LANGUAGE_MODES[target]["default_provider"]
    if default_pid in matches:
        matches.remove(default_pid)
        matches.insert(0, default_pid)
    return matches


def default_provider_for_mode(mode: Optional[str]) -> str:
    target = normalize_language_mode(mode)
    candidates = providers_for_mode(target)
    if candidates:
        return candidates[0]
    default_pid = LANGUAGE_MODES[target]["default_provider"]
    from .manager import ProviderManager

    return default_pid if ProviderManager.get_provider_class(default_pid) else "anime3rb"


def resolve_provider_id(
    mode: Optional[str],
    preferred_provider: Optional[str] = None,
    auto: bool = True,
) -> str:
    """Pick the provider for ``mode``.

    ``preferred_provider`` is honoured only when it supports the mode.  When it
    does not, the mode wins and the first compatible provider is used.  This is
    deliberately not a silent language fallback.
    """
    target = normalize_language_mode(mode)
    if preferred_provider and provider_supports_mode(preferred_provider, target):
        return _canonical(preferred_provider)
    return default_provider_for_mode(target)


def _canonical(provider_id: str) -> str:
    from .manager import ProviderManager

    cls = ProviderManager.get_provider_class(provider_id)
    return getattr(cls, "id", provider_id) if cls else provider_id
