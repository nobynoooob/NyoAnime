"""
Provider Manager for NyoAnime
Handles provider registry, discovery, dynamic switching, and lifecycle management.
"""

from typing import Dict, List, Type, Optional

from .base import BaseAnimeProvider
from .anime3rb import Anime3rbProvider
from .anime_slayer import AnimeSlayerProvider
from .hianime import HiAnimeDubProvider, HiAnimeProvider
from .animeify import AnimeifyProvider
from .stardima import StardimaProvider, StardimaKidsProvider
from .language import (
    LANGUAGE_MODES,
    normalize_language_mode,
    provider_language_mode,
    resolve_provider_id,
)


class ProviderManager:
    """Registry and factory for all available anime providers."""

    _PROVIDERS: Dict[str, Type[BaseAnimeProvider]] = {
        "anime3rb": Anime3rbProvider,
        "anime_slayer": AnimeSlayerProvider,
        "animeify": AnimeifyProvider,
        "animefy": AnimeifyProvider,
        "hianime": HiAnimeProvider,
        "hianime_sub": HiAnimeProvider,
        "hianime_dub": HiAnimeDubProvider,
        "stardima": StardimaProvider,
        "stardima_anime": StardimaProvider,
        "stardima_kids": StardimaKidsProvider,
        "stardima_cartoon": StardimaKidsProvider,
    }

    _INSTANCES: Dict[str, BaseAnimeProvider] = {}
    _ACTIVE_ID: str = "anime3rb"

    @classmethod
    def register_provider(cls, provider_id: str, provider_cls: Type[BaseAnimeProvider]):
        """Register a new anime provider."""
        cls._PROVIDERS[provider_id.lower()] = provider_cls

    @classmethod
    def get_provider_class(cls, provider_id: Optional[str]) -> Optional[Type[BaseAnimeProvider]]:
        if not provider_id:
            return None
        return cls._PROVIDERS.get(str(provider_id).lower())

    @classmethod
    def _canonical_id(cls, provider_id: Optional[str]) -> str:
        return cls.get_canonical_id(provider_id)

    @classmethod
    def get_canonical_id(cls, provider_id: Optional[str]) -> str:
        """Resolve an id or alias to the provider's canonical id."""
        pid = str(provider_id or cls._ACTIVE_ID).lower()
        provider_cls = cls._PROVIDERS.get(pid)
        if provider_cls is None:
            return "anime3rb"
        return getattr(provider_cls, "id", pid)

    @classmethod
    def get_provider(cls, provider_id: Optional[str] = None) -> BaseAnimeProvider:
        """Get an instantiated provider instance by ID (or currently active provider)."""
        pid = cls._canonical_id(provider_id)

        if pid not in cls._INSTANCES:
            provider_cls = cls._PROVIDERS.get(pid) or cls._PROVIDERS["anime3rb"]
            cls._INSTANCES[pid] = provider_cls()

        return cls._INSTANCES[pid]

    @classmethod
    def set_active_provider(cls, provider_id: str):
        """Set the active default provider. Aliases resolve to canonical IDs."""
        cls._ACTIVE_ID = cls._canonical_id(provider_id)

    @classmethod
    def get_active_provider_id(cls) -> str:
        """Get current active provider ID."""
        return cls._ACTIVE_ID

    @classmethod
    def resolve_active_provider(
        cls,
        settings,
        mode: Optional[str] = None,
        preferred_provider: Optional[str] = None,
        auto: Optional[bool] = None,
    ) -> str:
        """Resolve and return the provider id implied by the user's settings."""
        language_mode = normalize_language_mode(
            mode if mode is not None else settings.get("language_mode")
        )
        preferred = preferred_provider or settings.get("anime_provider")
        auto_select = settings.get("auto_provider", True) if auto is None else auto
        provider_id = resolve_provider_id(language_mode, preferred, auto_select)
        return cls._canonical_id(provider_id)

    @classmethod
    def sync_active_provider(cls, settings) -> str:
        """Apply the resolved provider to settings and the registry."""
        provider_id = cls.resolve_active_provider(settings)
        if settings.get("anime_provider") != provider_id:
            settings.set("anime_provider", provider_id)
        if settings.get("language_mode") not in LANGUAGE_MODES:
            settings.set("language_mode", provider_language_mode(provider_id))
        cls.set_active_provider(provider_id)
        return provider_id

    @classmethod
    def list_providers(cls) -> List[Dict[str, str]]:
        """List all available providers with their metadata."""
        seen = set()
        providers = []
        for pid, p_cls in cls._PROVIDERS.items():
            if p_cls in seen:
                continue
            seen.add(p_cls)
            providers.append(
                {
                    "id": getattr(p_cls, "id", pid),
                    "name": getattr(p_cls, "name", pid.title()),
                    "description": getattr(p_cls, "description", ""),
                    "language": getattr(p_cls, "language", ""),
                    "language_mode": getattr(p_cls, "language_mode", ""),
                }
            )
        return providers

    @classmethod
    def get_available_providers(cls) -> List[Dict[str, str]]:
        """Alias for list_providers for backward compatibility."""
        return cls.list_providers()

    @classmethod
    def get_provider_choices(cls) -> List[str]:
        """Get canonical provider IDs (aliases are still accepted by get_provider)."""
        seen = set()
        choices = []
        for pid, provider_cls in cls._PROVIDERS.items():
            canonical = getattr(provider_cls, "id", pid)
            if canonical not in seen:
                seen.add(canonical)
                choices.append(canonical)
        return choices

    @classmethod
    def get_provider_choices_for_mode(cls, mode: str) -> List[str]:
        """Get canonical provider IDs compatible with a language mode."""
        target = normalize_language_mode(mode)
        return [
            pid
            for pid in cls.get_provider_choices()
            if getattr(cls.get_provider_class(pid), "language_mode", "") == target
        ]
