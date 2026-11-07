"""Concurrency, timeout and cache behaviour for cross-provider search."""

import time

import pytest

from nyoanime.models import AnimeResult
from nyoanime.providers.manager import ProviderManager
from nyoanime.providers.search import clear_search_cache, search_providers


class _FakeProvider:
    def __init__(self, provider_id, results=None, delay=0.0, error=None):
        self.id = provider_id
        self._results = results or []
        self._delay = delay
        self._error = error
        self.calls = 0

    def search_anime(self, query):
        self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return [
            AnimeResult(id=f"{self.id}-{index}", title_en=f"{query} {index}")
            for index in range(self._results)
        ]


@pytest.fixture
def providers(monkeypatch):
    instances = {}

    monkeypatch.setattr(ProviderManager, "get_canonical_id", classmethod(lambda cls, pid: str(pid)))
    monkeypatch.setattr(
        ProviderManager,
        "get_provider_class",
        classmethod(lambda cls, pid: _FakeProvider if str(pid) in instances else None),
    )
    monkeypatch.setattr(
        ProviderManager,
        "get_provider",
        classmethod(lambda cls, pid=None: instances.get(str(pid))),
    )

    def register(provider_id, **kwargs):
        fake = _FakeProvider(provider_id, **kwargs)
        instances[provider_id] = fake
        return fake

    return register


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_search_cache()
    yield
    clear_search_cache()


def test_providers_are_searched_concurrently(providers):
    providers("fast-a", results=1, delay=0.2)
    providers("fast-b", results=1, delay=0.2)
    providers("fast-c", results=1, delay=0.2)

    started = time.monotonic()
    results = search_providers(
        "naruto",
        ["fast-a", "fast-b", "fast-c"],
        timeout=2.0,
        use_cache=False,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"searches ran sequentially ({elapsed:.2f}s)"
    assert {item.provider_id for item in results} == {"fast-a", "fast-b", "fast-c"}


def test_timeout_and_failure_return_partial_results(providers):
    providers("good", results=2, delay=0.01)
    providers("dead", delay=2.0)
    providers("broken", error=RuntimeError("boom"))

    started = time.monotonic()
    results = search_providers(
        "one piece",
        ["good", "dead", "broken"],
        timeout=0.25,
        use_cache=False,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.8, f"a dead provider blocked the search ({elapsed:.2f}s)"
    assert [item.provider_id for item in results] == ["good", "good"]


def test_results_are_annotated_and_duplicate_providers_collapsed(providers):
    fake = providers("dup", results=2)
    results = search_providers("bleach", ["dup", "dup"], use_cache=False)
    assert [item.id for item in results] == ["dup-0", "dup-1"]
    assert all(item.provider_id == "dup" for item in results)
    assert fake.calls == 1


def test_repeated_queries_hit_the_ttl_cache(providers):
    fake = providers("cached", results=1)

    first = search_providers("aot", ["cached"])
    second = search_providers("aot", ["cached"])

    assert fake.calls == 1
    assert [item.id for item in first] == [item.id for item in second]
    # Cached results are copied so the UI can mutate them safely.
    assert first[0] is not second[0]

    clear_search_cache()
    search_providers("aot", ["cached"])
    assert fake.calls == 2


def test_empty_query_short_circuits(providers):
    fake = providers("any", results=1)
    assert search_providers("   ", ["any"]) == []
    assert fake.calls == 0


def test_progress_callback_runs_for_every_provider(providers):
    providers("p1", results=1)
    providers("p2", results=2)
    seen = []

    search_providers(
        "callback",
        ["p1", "p2"],
        use_cache=False,
        on_provider_done=lambda pid, count: seen.append((pid, count)),
    )

    assert sorted(seen) == [("p1", 1), ("p2", 2)]
