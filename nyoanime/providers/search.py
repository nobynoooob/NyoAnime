"""
Concurrent multi-provider search with a small TTL cache.

Provider scrapers are synchronous, so a thread pool is the least invasive way
to fan out. Each provider gets a hard deadline and any failure is local to that
provider: partial results are always returned.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..logger import logger
from ..models import AnimeResult
from .language import normalize_language_mode, providers_for_mode
from .manager import ProviderManager

DEFAULT_SEARCH_TIMEOUT = 5.0
DEFAULT_SEARCH_WORKERS = 4
DEFAULT_CACHE_TTL = 120.0
DEFAULT_CACHE_SIZE = 64

ProviderDoneCallback = Callable[[str, int], None]


class TTLCache:
    """Bounded, thread-safe TTL cache. Deliberately tiny; no dependencies."""

    def __init__(self, ttl: float = DEFAULT_CACHE_TTL, maxsize: int = DEFAULT_CACHE_SIZE):
        self.ttl = float(ttl)
        self.maxsize = max(1, int(maxsize))
        self._lock = threading.Lock()
        self._data: Dict[Tuple, Tuple[float, List[AnimeResult]]] = {}

    def get(self, key: Tuple):
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if now >= expires_at:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: Tuple, value: List[AnimeResult]):
        now = time.monotonic()
        with self._lock:
            if len(self._data) >= self.maxsize:
                # Drop the entry closest to expiry; cheap and good enough here.
                oldest = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(oldest, None)
            self._data[key] = (now + self.ttl, value)

    def clear(self):
        with self._lock:
            self._data.clear()


_SEARCH_CACHE = TTLCache()


def clear_search_cache():
    _SEARCH_CACHE.clear()


def _resolve_provider_ids(
    provider_ids: Optional[Sequence[str]],
    mode: Optional[str],
) -> List[str]:
    if provider_ids is None:
        if mode is not None:
            return providers_for_mode(mode)
        return [ProviderManager.get_active_provider_id()]

    resolved: List[str] = []
    seen = set()
    for provider_id in provider_ids:
        canonical = ProviderManager.get_canonical_id(provider_id)
        if canonical in seen or ProviderManager.get_provider_class(canonical) is None:
            continue
        seen.add(canonical)
        resolved.append(canonical)
    return resolved


def _run_provider_search(provider_id: str, query: str) -> List[AnimeResult]:
    provider = ProviderManager.get_provider(provider_id)
    results = provider.search_anime(query) or []
    annotated: List[AnimeResult] = []
    for item in results:
        if not isinstance(item, AnimeResult):
            continue
        # Copy so provider caches are never tagged or mutated by the UI.
        annotated.append(replace(item, provider_id=provider_id))
    return annotated


def search_providers(
    query: str,
    provider_ids: Optional[Sequence[str]] = None,
    *,
    mode: Optional[str] = None,
    timeout: float = DEFAULT_SEARCH_TIMEOUT,
    max_workers: int = DEFAULT_SEARCH_WORKERS,
    use_cache: bool = True,
    on_provider_done: Optional[ProviderDoneCallback] = None,
) -> List[AnimeResult]:
    """Search providers concurrently and merge their results.

    ``provider_ids`` wins over ``mode``; when both are omitted the active
    provider is searched. Failures, exceptions and timeouts are logged and
    skipped, so a dead site can never hold up the rest of the results.
    """
    query = (query or "").strip()
    if not query:
        return []

    if mode is not None:
        mode = normalize_language_mode(mode)
    targets = _resolve_provider_ids(provider_ids, mode)
    if not targets:
        return []

    cache_key = (query.lower(), tuple(targets))
    if use_cache:
        cached = _SEARCH_CACHE.get(cache_key)
        if cached is not None:
            if on_provider_done is not None:
                for provider_id in targets:
                    on_provider_done(provider_id, sum(1 for r in cached if r.provider_id == provider_id))
            return [replace(item) for item in cached]

    by_provider: Dict[str, List[AnimeResult]] = {}
    executor = ThreadPoolExecutor(
        max_workers=max(1, min(int(max_workers), len(targets))),
        thread_name_prefix="nyo-search",
    )
    futures = {
        executor.submit(_run_provider_search, provider_id, query): provider_id
        for provider_id in targets
    }

    try:
        wait(futures, timeout=max(0.1, float(timeout)))
        for future in list(futures):
            provider_id = futures[future]
            if not future.done():
                future.cancel()
                logger.warning("SEARCH", f"{provider_id} timed out after {timeout:.1f}s")
                if on_provider_done is not None:
                    _notify(on_provider_done, provider_id, 0)
                continue
            try:
                results = future.result()
            except Exception as exc:
                logger.warning("SEARCH", f"{provider_id} failed: {exc}")
                results = []
            by_provider[provider_id] = results
            if on_provider_done is not None:
                _notify(on_provider_done, provider_id, len(results))
    finally:
        # Never block the loading screen on a hung provider thread.
        executor.shutdown(wait=False, cancel_futures=True)

    merged: List[AnimeResult] = []
    seen = set()
    for provider_id in targets:
        for item in by_provider.get(provider_id, []):
            key = (provider_id, item.id)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)

    if use_cache and merged:
        _SEARCH_CACHE.set(cache_key, merged)
    return [replace(item) for item in merged]


def _notify(callback: ProviderDoneCallback, provider_id: str, count: int):
    try:
        callback(provider_id, count)
    except Exception:
        pass
