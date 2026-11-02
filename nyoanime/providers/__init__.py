"""
nyoanime Providers Package
"""

from .base import BaseAnimeProvider
from .anime3rb import Anime3rbProvider
from .anime_slayer import AnimeSlayerProvider
from .hianime import HiAnimeDubProvider, HiAnimeProvider
from .animeify import AnimeifyProvider
from .stardima import StardimaProvider, StardimaKidsProvider
from .language import (
    DEFAULT_LANGUAGE_MODE,
    LANGUAGE_MODES,
    MODE_CHOICES,
    language_badge,
    language_mode_label,
    normalize_language_mode,
    providers_for_mode,
    resolve_provider_id,
)
from .manager import ProviderManager

__all__ = [
    "BaseAnimeProvider",
    "Anime3rbProvider",
    "AnimeSlayerProvider",
    "HiAnimeProvider",
    "HiAnimeDubProvider",
    "AnimeifyProvider",
    "StardimaProvider",
    "StardimaKidsProvider",
    "ProviderManager",
    "DEFAULT_LANGUAGE_MODE",
    "LANGUAGE_MODES",
    "MODE_CHOICES",
    "language_badge",
    "language_mode_label",
    "normalize_language_mode",
    "providers_for_mode",
    "resolve_provider_id",
]
