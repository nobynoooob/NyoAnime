"""Watch Together lobby helpers.

Each member verifies which language modes can actually play the picked episode
before the room starts. The checks are deliberately local: a provider may work
for one member (region, credentials, CDN) and fail for another, and a room
should never advertise a stream nobody can open.
"""
from __future__ import annotations

import asyncio
import difflib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..providers.language import (
    LANGUAGE_MODES,
    MODE_CHOICES,
    normalize_language_mode,
    providers_for_mode,
)
from ..providers.manager import ProviderManager

# Defaults chosen to fail over quickly instead of holding the lobby hostage.
DEFAULT_PROBE_TIMEOUT = 12.0
FALLBACK_PREFERENCE = ("en_sub", "en_dub", "ar_sub", "ar_dub", "cartoon")
_CARTOON_HINTS = ("cartoon", "kids", "kid", "children")
_HTTP_PREFIXES = ("http://", "https://")


@dataclass
class LobbyOption:
    mode: str
    provider_id: str
    playback: Dict[str, Any]

    @property
    def mode_label(self) -> str:
        return LANGUAGE_MODES.get(self.mode, {}).get("label", self.mode)

    @property
    def provider_name(self) -> str:
        provider_cls = ProviderManager.get_provider_class(self.provider_id)
        return getattr(provider_cls, "name", self.provider_id)

    @property
    def label(self) -> str:
        return f"{self.mode_label} · {self.provider_name}"

    def public(self) -> Dict[str, str]:
        return {
            "mode": self.mode,
            "provider_id": self.provider_id,
            "label": self.label,
        }


@dataclass
class LobbyMember:
    key: str
    name: str
    role: str = "guest"
    ready: bool = False
    mode: str = ""
    is_you: bool = False

    @property
    def mode_label(self) -> str:
        if not self.mode:
            return ""
        return LANGUAGE_MODES.get(self.mode, {}).get("label", self.mode)


@dataclass
class LobbyState:
    reference: Dict[str, str]
    lobby_id: str
    deadline: float
    seed_mode: str = ""
    seed_playback: Optional[Dict[str, Any]] = None
    options: List[LobbyOption] = field(default_factory=list)
    selected: Optional[LobbyOption] = None
    choice_index: int = 0
    discovering: bool = True
    ready: bool = False
    started: bool = False
    error: str = ""
    members: Dict[str, LobbyMember] = field(default_factory=dict)

    @property
    def seed_option(self) -> Optional[LobbyOption]:
        if not self.seed_playback or not self.seed_mode:
            return None
        return LobbyOption(self.seed_mode, self._seed_provider_id(), self.seed_playback)

    def _seed_provider_id(self) -> str:
        provider_id = str(self.reference.get("provider_id") or "")
        if provider_id:
            return ProviderManager.get_canonical_id(provider_id)
        return str(LANGUAGE_MODES.get(self.seed_mode, {}).get("default_provider", ""))

    @property
    def fallback(self) -> Optional[LobbyOption]:
        return choose_fallback(self.options) or self.seed_option

    @property
    def all_ready(self) -> bool:
        return bool(self.members) and all(member.ready for member in self.members.values())

    def remaining(self, now: Optional[float] = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, float(self.deadline) - current)

    def can_start_anyway(self, now: Optional[float] = None) -> bool:
        return self.all_ready or self.remaining(now) <= 0.0


def content_is_cartoon(anime_type: Any) -> bool:
    """Cartoon providers are only safe for cartoon/kids catalogues."""
    text = str(anime_type or "").strip().lower()
    return any(hint in text for hint in _CARTOON_HINTS)


def allowed_modes(anime_type: Any) -> List[str]:
    """Language modes worth probing for this content.

    Cartoon is an Arabic-dub kids catalogue, so it is only probed for
    cartoon/kids content. Everything else is safe for both catalogue types.
    """
    cartoon = content_is_cartoon(anime_type)
    return [
        mode for mode in MODE_CHOICES
        if mode != "cartoon" or cartoon
    ]


def choose_fallback(options: Sequence[LobbyOption]) -> Optional[LobbyOption]:
    """English sub first, then the remaining verified modes in order."""
    if not options:
        return None
    by_mode: Dict[str, LobbyOption] = {}
    for option in options:
        by_mode.setdefault(normalize_language_mode(option.mode), option)
    for mode in FALLBACK_PREFERENCE:
        option = by_mode.get(mode)
        if option is not None:
            return option
    return options[0]


def _resolution(value: Any) -> int:
    match = re.search(
        r"(\d{3,4})p",
        f"{getattr(value, 'res', '')} {getattr(value, 'name', '')}",
    )
    return int(match.group(1)) if match else 0


def _stream_url(quality: Any, current: Dict[str, Any]) -> str:
    url = (
        getattr(quality, "direct_url", "")
        or getattr(quality, "url", "")
        or current.get(getattr(quality, "server_key", ""), "")
    )
    return str(url or "").strip()


def pick_quality(server_data: Dict[str, Any]) -> Optional[Any]:
    qualities = [
        quality for quality in (server_data.get("Qualities") or [])
        if _stream_url(quality, server_data.get("CurrentEpisode") or {})
    ]
    if not qualities:
        return None
    return max(qualities, key=_resolution)


def playback_from_server_data(
    server_data: Dict[str, Any],
    *,
    anime_title: str,
    anime_id: str,
    episode_number: str,
    episode_title: str,
    provider_id: str,
    anime_type: str = "SERIES",
) -> Optional[Dict[str, Any]]:
    """Convert a provider's server payload into the room playback shape."""
    if not isinstance(server_data, dict):
        return None
    choice = pick_quality(server_data)
    if choice is None:
        return None
    current = server_data.get("CurrentEpisode") or {}
    direct_url = _stream_url(choice, current)
    if not direct_url.startswith(_HTTP_PREFIXES):
        return None

    audio_tracks = list(getattr(choice, "audio_tracks", []) or [])
    master_url = str(getattr(choice, "master_url", "") or "")
    playback_url = master_url if audio_tracks and master_url else direct_url
    bitrate = int(getattr(choice, "bandwidth", 0) or 0) if playback_url == master_url else 0

    quality_match = re.search(r"\b(\d{3,4}p)\b", str(getattr(choice, "name", "")))
    quality_tag = quality_match.group(1) if quality_match else str(getattr(choice, "name", ""))
    display = str(episode_number or "").strip()
    title = (
        f"{anime_title} - Ep {display}"
        if anime_title and display
        else (anime_title or f"Episode {display}")
    )

    return {
        "url": playback_url,
        "title": title,
        "episode_number": display,
        "episode_title": str(episode_title or ""),
        "headers": dict(getattr(choice, "headers", None) or {}),
        "start_time": 0.0,
        "audio_lang": getattr(choice, "audio_lang", "") or "",
        "subtitle_url": getattr(choice, "subtitle_url", "") or "",
        "subtitle_lang": getattr(choice, "subtitle_lang", "") or "",
        "audio_url": getattr(choice, "audio_url", "") or "",
        "subtitles_disabled": False,
        "hls_bitrate": bitrate,
        "quality": quality_tag,
        "provider_id": provider_id,
        "anime_id": anime_id,
    }


def _normalize_title(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _title_score(candidate: Any, target: str) -> float:
    if not target:
        return 0.0
    best = 0.0
    for attr in ("title_en", "title_romaji", "title_ar", "title_jp"):
        name = _normalize_title(getattr(candidate, attr, ""))
        if not name:
            continue
        if name == target:
            return 1.0
        if target in name or name in target:
            best = max(best, 0.9)
            continue
        best = max(best, difflib.SequenceMatcher(None, target, name).ratio())
    return best


def match_anime(results: Sequence[Any], title: str) -> Optional[Any]:
    target = _normalize_title(title)
    if not target:
        return None
    best = None
    best_score = 0.0
    for candidate in results or []:
        score = _title_score(candidate, target)
        if score > best_score:
            best, best_score = candidate, score
    if best is None or best_score < 0.6:
        return None
    return best


def find_episode(episodes: Sequence[Any], episode_number: Any) -> Optional[Any]:
    target = str(episode_number or "").strip()
    if not target:
        return None
    for episode in episodes or []:
        if str(getattr(episode, "number", "")).strip() == target:
            return episode
        if str(getattr(episode, "display_num", "")).strip() == target:
            return episode
    digits = re.search(r"\d+", target)
    if digits:
        numeric = int(digits.group(0))
        for episode in episodes or []:
            try:
                if int(getattr(episode, "display_num", 0)) == numeric:
                    return episode
            except (TypeError, ValueError):
                continue
    return None


def probe_provider(
    mode: str,
    provider_id: str,
    reference: Dict[str, Any],
) -> Optional[LobbyOption]:
    """Blocking probe: search provider, match episode, resolve a playback payload."""
    provider_cls = ProviderManager.get_provider_class(provider_id)
    if provider_cls is None:
        return None
    advertised = normalize_language_mode(
        getattr(provider_cls, "language_mode", "") or mode
    )
    if advertised != normalize_language_mode(mode):
        return None

    title = str(reference.get("title") or "").strip()
    episode_number = str(reference.get("episode_number") or "").strip()
    if not title or not episode_number:
        return None

    provider = ProviderManager.get_provider(provider_id)
    canonical = ProviderManager.get_canonical_id(provider_id)
    anime_type = str(reference.get("anime_type") or "SERIES")

    anime_id = ""
    anime_title = title
    reference_provider = str(reference.get("provider_id") or "")
    if (
        reference_provider
        and ProviderManager.get_canonical_id(reference_provider) == canonical
    ):
        anime_id = str(reference.get("anime_id") or "")

    if not anime_id:
        anime = match_anime(provider.search_anime(title) or [], title)
        if anime is None:
            return None
        anime_id = str(getattr(anime, "id", "") or "")
        anime_title = str(getattr(anime, "title_en", "") or title)
        anime_type = str(getattr(anime, "type", "") or anime_type)

    episodes = provider.get_episodes(anime_id) or []
    episode = find_episode(episodes, episode_number)
    if episode is None:
        return None

    server_data = provider.get_streaming_servers(
        anime_id,
        str(getattr(episode, "number", "") or episode_number),
        anime_type,
    )
    playback = playback_from_server_data(
        server_data or {},
        anime_title=anime_title,
        anime_id=anime_id,
        episode_number=episode_number,
        episode_title=str(getattr(episode, "title", "") or ""),
        provider_id=canonical,
        anime_type=anime_type,
    )
    if playback is None:
        return None
    return LobbyOption(
        mode=normalize_language_mode(mode),
        provider_id=canonical,
        playback=playback,
    )


async def discover_options(
    reference: Dict[str, Any],
    *,
    probe: Optional[Callable[[str, str, Dict[str, Any]], Optional[LobbyOption]]] = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    modes: Optional[Sequence[str]] = None,
) -> List[LobbyOption]:
    """Return one verified option per language mode, in room display order."""
    if not isinstance(reference, dict):
        return []
    selected_modes = list(modes) if modes is not None else allowed_modes(
        reference.get("anime_type")
    )
    probe_fn = probe or probe_provider
    timeout = max(0.1, float(timeout))
    loop = asyncio.get_running_loop()

    async def probe_mode(mode: str) -> Optional[LobbyOption]:
        # One budget per mode: a mode with several providers must not take
        # longer than a single provider timeout in total.
        stop_at = loop.time() + timeout
        for provider_id in providers_for_mode(mode):
            left = stop_at - loop.time()
            if left <= 0.1:
                break
            try:
                option = await asyncio.wait_for(
                    asyncio.to_thread(probe_fn, mode, provider_id, reference),
                    timeout=left,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A provider that times out or throws is simply not available
                # for this member. The UI stays responsive either way.
                continue
            if isinstance(option, LobbyOption):
                return option
        return None

    results = await asyncio.gather(
        *(probe_mode(mode) for mode in selected_modes),
        return_exceptions=True,
    )
    options = [result for result in results if isinstance(result, LobbyOption)]
    order = {mode: index for index, mode in enumerate(selected_modes)}
    options.sort(key=lambda option: order.get(option.mode, len(order)))
    return options


def preferred_fallback_mode(anime_type: Any = None, available: Sequence[str] = ()) -> str:
    """Best-effort fallback mode advertised in the lobby state."""
    modes = set(available) if available else set(allowed_modes(anime_type))
    for mode in FALLBACK_PREFERENCE:
        if mode in modes:
            return mode
    return next(iter(modes), "en_sub")
