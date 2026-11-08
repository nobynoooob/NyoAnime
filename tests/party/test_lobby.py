import pytest

from nyoanime.models import AnimeResult, Episode, QualityOption
from nyoanime.party.lobby import (
    LobbyOption,
    LobbyState,
    allowed_modes,
    choose_fallback,
    content_is_cartoon,
    discover_options,
    find_episode,
    match_anime,
    playback_from_server_data,
    probe_provider,
)
from nyoanime.providers.manager import ProviderManager


def _option(mode, provider="hianime", url="https://cdn.example/ep.m3u8"):
    return LobbyOption(
        mode=mode,
        provider_id=provider,
        playback={"url": url, "title": "Frieren - Ep 3"},
    )


def test_cartoon_provider_is_hidden_for_anime_content():
    assert content_is_cartoon("TV") is False
    assert "cartoon" not in allowed_modes("TV")
    assert "cartoon" not in allowed_modes("Movie")
    assert "cartoon" in allowed_modes("Cartoon")


def test_cartoon_content_keeps_other_languages():
    modes = allowed_modes("Kids")
    assert "cartoon" in modes
    assert "en_sub" in modes


def test_fallback_prefers_english_sub_then_other_verified_modes():
    options = [_option("ar_sub"), _option("en_dub"), _option("en_sub")]

    assert choose_fallback(options).mode == "en_sub"
    assert choose_fallback([_option("ar_dub"), _option("ar_sub")]).mode == "ar_sub"
    assert choose_fallback([_option("cartoon")]).mode == "cartoon"
    assert choose_fallback([]) is None


@pytest.mark.asyncio
async def test_discover_options_skips_modes_that_cannot_play():
    calls = []

    def probe(mode, provider_id, reference):
        calls.append((mode, provider_id))
        if mode == "en_sub":
            return _option("en_sub", provider_id)
        return None

    reference = {
        "title": "Frieren",
        "episode_number": "3",
        "anime_type": "TV",
    }
    options = await discover_options(reference, probe=probe, timeout=1.0)

    assert [option.mode for option in options] == ["en_sub"]
    assert ("en_sub", "hianime") in calls
    assert all(mode != "cartoon" for mode, _ in calls)


@pytest.mark.asyncio
async def test_discover_options_survives_probe_errors():
    def probe(mode, provider_id, reference):
        if mode == "ar_sub":
            raise RuntimeError("provider exploded")
        return _option(mode, provider_id)

    options = await discover_options(
        {"title": "Frieren", "episode_number": "1", "anime_type": "TV"},
        probe=probe,
        timeout=1.0,
    )

    assert [option.mode for option in options] == ["ar_dub", "en_sub", "en_dub"]


def test_match_anime_and_find_episode_are_forgiving_about_labels():
    results = [
        AnimeResult(id="other", title_en="Frieren: Beyond Journey's End"),
    ]
    anime = match_anime(results, "Frieren: Beyond Journeys End")
    assert anime is results[0]
    assert match_anime(results, "Naruto") is None

    episodes = [Episode(number="ep-3", display_num=3)]
    assert find_episode(episodes, "3") is episodes[0]
    assert find_episode(episodes, "ep-3") is episodes[0]
    assert find_episode(episodes, "9") is None


def test_playback_from_server_data_picks_highest_quality():
    data = {
        "Qualities": [
            QualityOption(name="720p", direct_url="https://cdn.example/720.m3u8"),
            QualityOption(name="1080p", direct_url="https://cdn.example/1080.m3u8"),
        ],
        "CurrentEpisode": {},
    }

    playback = playback_from_server_data(
        data,
        anime_title="Frieren",
        anime_id="frieren",
        episode_number="3",
        episode_title="Killing Magic",
        provider_id="hianime",
    )

    assert playback["url"] == "https://cdn.example/1080.m3u8"
    assert playback["episode_number"] == "3"
    assert playback["provider_id"] == "hianime"
    assert playback["quality"] == "1080p"


def test_probe_provider_matches_title_and_builds_a_playable_option(monkeypatch):
    class FakeProvider:
        id = "fake"
        name = "Fake"
        language_mode = "en_sub"

        def search_anime(self, title):
            return [AnimeResult(id="frieren", title_en="Frieren", type="TV")]

        def get_episodes(self, anime_id):
            return [Episode(number="3", display_num=3, title="Killing Magic")]

        def get_streaming_servers(self, anime_id, episode_num, anime_type="SERIES"):
            return {
                "Qualities": [
                    QualityOption(
                        name="1080p",
                        direct_url="https://cdn.example/frieren-3.m3u8",
                        headers={"Referer": "https://provider.example/"},
                        audio_lang="jpn,ja",
                    )
                ],
                "CurrentEpisode": {},
            }

    monkeypatch.setattr(
        ProviderManager, "get_provider_class", staticmethod(lambda pid: FakeProvider)
    )
    monkeypatch.setattr(
        ProviderManager, "get_provider", staticmethod(lambda pid=None: FakeProvider())
    )
    monkeypatch.setattr(
        ProviderManager, "get_canonical_id", staticmethod(lambda pid: str(pid))
    )

    option = probe_provider(
        "en_sub",
        "fake",
        {
            "title": "Frieren",
            "episode_number": "3",
            "anime_type": "TV",
        },
    )

    assert isinstance(option, LobbyOption)
    assert option.mode == "en_sub"
    assert option.playback["url"] == "https://cdn.example/frieren-3.m3u8"
    assert option.playback["headers"]["Referer"] == "https://provider.example/"
    assert option.playback["episode_number"] == "3"


def test_lobby_state_uses_seed_playback_as_last_resort_fallback():
    playback = {"url": "https://cdn.example/seed.m3u8", "title": "Frieren - Ep 3"}
    lobby = LobbyState(
        reference={
            "title": "Frieren",
            "episode_number": "3",
            "provider_id": "hianime",
            "language_mode": "en_sub",
        },
        lobby_id="x",
        deadline=0.0,
        seed_mode="en_sub",
        seed_playback=playback,
    )

    assert lobby.fallback is not None
    assert lobby.fallback.playback is playback
    assert lobby.can_start_anyway() is True
