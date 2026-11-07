"""Offline tests for the HiAnime English provider and language-mode plumbing."""

import base64
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from nyoanime.models import Episode, QualityOption
from nyoanime.providers.hianime import HiAnimeDubProvider, HiAnimeProvider
from nyoanime.providers.language import (
    language_mode_label,
    mode_audio_lang,
    normalize_language_mode,
    providers_for_mode,
    resolve_provider_id,
)
from nyoanime.providers.manager import ProviderManager
from nyoanime.settings import SettingsManager


SEARCH_HTML = """
<div class="film_list-wrap">
  <div class="flw-item flw-item-big">
    <div class="film-poster">
      <div class="tick ltr">
        <div class="tick-item tick-sub"><i class="fas"></i>220</div>
        <div class="tick-item tick-dub"><i class="fas"></i>220</div>
        <div class="tick-item tick-eps">220</div>
      </div>
      <img src="https://cdn.example/naruto.jpg" class="film-poster-img" alt="Naruto">
      <a href="https://hianime.at/watch/naruto-1335" title="Naruto" data-id="1335"></a>
    </div>
    <div class="film-detail">
      <h3 class="film-name">
        <a href="https://hianime.at/naruto-1335" title="Naruto"
           class="dynamic-name" data-jname="Naruto">Naruto</a>
      </h3>
      <div class="fd-infor">
        <span class="fdi-item">TV</span>
        <span class="fdi-item fdi-duration">23m</span>
      </div>
    </div>
  </div>
  <div class="flw-item">
    <h3 class="film-name">
      <a href="https://hianime.at/naruto-1335" title="Naruto Duplicate"></a>
    </h3>
  </div>
</div>
"""

DETAILS_HTML = """
<h2 class="film-name dynamic-name" data-jname="Naruto">Naruto</h2>
<div class="film-stats">
  <div class="tick-item tick-pg">PG-13</div>
  <div class="tick-item tick-sub"><i></i>220</div>
  <div class="tick-item tick-dub"><i></i>219</div>
  <div class="tick-item tick-eps">220</div>
  <span class="item">TV</span><span class="item">23m</span>
</div>
<div class="anisc-poster">
  <div class="film-poster"><img src="https://cdn.example/poster.jpg" class="film-poster-img"></div>
</div>
<div class="film-description m-hide"><div class="text">A ninja story.</div></div>
<div class="item item-title"><span class="item-head">Japanese:</span><span class="name">ナルト</span></div>
<div class="item item-title"><span class="item-head">Aired:</span><span class="name">Oct 3, 2002 to Feb 8, 2007</span></div>
<div class="item item-title"><span class="item-head">Duration:</span><span class="name">23m</span></div>
<div class="item item-title"><span class="item-head">Status:</span><span class="name">Finished Airing</span></div>
<div class="item item-title"><span class="item-head">MAL Score:</span><span class="name">7.89</span></div>
<div class="item item-list"><span class="item-head">Genres:</span>
  <a href="/genres/action" title="Action">Action</a>
  <a href="/genres/fantasy" title="Fantasy">Fantasy</a>
</div>
<div class="item item-title"><span class="item-head">Studios:</span>
  <a class="name" href="/studios/pierrot" title="Studio Pierrot">Studio Pierrot</a>
</div>
<div class="item item-title"><span class="item-head">Producers:</span>
  <a class="name" href="/producers/aniplex" title="Aniplex">Aniplex</a>
</div>
<div class="clearfix"></div>
"""

EPISODES_JSON = {
    "status": True,
    "html": """
    <div id="episodes-page-1">
      <a title="Episode 1" class="ssl-item ep-item" data-number="1"
         data-id="111" href="https://hianime.at/watch/naruto-1335?ep=111">
        <div class="ep-name">Episode 1</div>
      </a>
      <a title="Episode 2" class="ssl-item ep-item" data-number="2"
         data-id="222" href="https://hianime.at/watch/naruto-1335?ep=222">
        <div class="ep-name">Episode 2</div>
      </a>
    </div>
    """,
}

EMBED_URL = "https://zokoanime.video/stream/mal/20/1/sub"
DUB_EMBED_URL = "https://zokoanime.video/stream/mal/20/1/dub"
MASTER_URL = "https://hls.example/master.m3u8"
MASTER_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:4
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,URI="audio/en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1920x1080,AUDIO="aud"
1080/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=1280x720,AUDIO="aud"
720/index.m3u8
"""
SUBTITLES = [
    {"lang": "en", "label": "English", "default": True, "src": "https://hls.example/en.vtt"},
    {"lang": "ar", "label": "Arabic", "default": False, "src": "https://hls.example/ar.vtt"},
]


def _encode_blob(payload):
    raw = json.dumps(payload).encode()
    key = b"otaku-embed-v1"
    data = bytes(byte ^ key[i % len(key)] for i, byte in enumerate(raw))
    return base64.b64encode(data).decode()


def _embed_page(payload):
    return f'<script>window.__P="{_encode_blob(payload)}"</script>'


def _servers_html():
    sub = base64.b64encode(EMBED_URL.encode()).decode()
    dub = base64.b64encode(DUB_EMBED_URL.encode()).decode()
    return f"""
    <div class="item server-item" data-type="sub" data-server-name="ZokoAnime" data-hash="{sub}"></div>
    <div class="item server-item" data-type="dub" data-server-name="ZokoAnime" data-hash="{dub}"></div>
    """


def make_provider(cls=HiAnimeProvider):
    provider = cls()
    provider._availability["naruto-1335"] = {"sub": 220, "dub": 219}
    provider._episode_cache["naruto-1335"] = [
        Episode(
            number="1",
            display_num=1,
            title="Episode 1",
            url="https://hianime.at/watch/naruto-1335?ep=111",
            anime_id="naruto-1335",
        ),
        Episode(
            number="2",
            display_num=2,
            title="Episode 2",
            url="https://hianime.at/watch/naruto-1335?ep=222",
            anime_id="naruto-1335",
        ),
    ]
    return provider


class TestSearchAndMetadata:
    def test_search_parses_titles_posters_and_deduplicates(self):
        provider = HiAnimeProvider()
        with mock.patch.object(provider, "_get_text", return_value=SEARCH_HTML):
            results = provider.search_anime("naruto")

        assert len(results) == 1
        assert results[0].id == "naruto-1335"
        assert results[0].title_en == "Naruto"
        assert results[0].thumbnail == "https://cdn.example/naruto.jpg"
        assert results[0].episodes == "220"
        assert results[0].language == "sub"

    def test_details_are_normalized(self):
        provider = HiAnimeProvider()
        with mock.patch.object(provider, "_get_text", return_value=DETAILS_HTML):
            details = provider.get_anime_details("naruto-1335")

        assert details.title_en == "Naruto"
        assert details.title_jp == "Naruto"
        assert details.status == "Finished Airing"
        assert details.score == "7.89"
        assert details.studio == "Studio Pierrot"
        assert details.genres == "Action, Fantasy"
        assert details.synopsis == "A ninja story."
        assert details.thumbnail == "https://cdn.example/poster.jpg"
        assert provider._availability["naruto-1335"] == {"sub": 220, "dub": 219}

    def test_episode_numbers_ids_and_availability(self):
        provider = HiAnimeProvider()
        with mock.patch.object(
            provider,
            "_get_json",
            return_value=EPISODES_JSON,
        ), mock.patch.object(provider, "_get_text", return_value=DETAILS_HTML):
            episodes = provider.get_episodes("naruto-1335")

        assert [ep.number for ep in episodes] == ["1", "2"]
        assert episodes[0].url.endswith("ep=111")
        assert episodes[0].available_modes == ["sub", "dub"]
        assert episodes[0].language == "sub"

    def test_dub_provider_marks_sub_only_episodes(self):
        provider = HiAnimeDubProvider()
        provider._availability["naruto-1335"] = {"sub": 220, "dub": 1}
        with mock.patch.object(provider, "_get_json", return_value=EPISODES_JSON), mock.patch.object(
            provider, "_get_text", return_value=DETAILS_HTML
        ):
            episodes = provider.get_episodes("naruto-1335")

        assert episodes[0].language == "dub"
        assert episodes[1].language == "sub"
        assert episodes[1].available_modes == ["sub"]


class TestStreamResolution:
    def _patch_streams(self, provider, embed_url=EMBED_URL, server_html=None):
        payload = {"src": MASTER_URL, "subtitles": SUBTITLES}
        get_json = mock.Mock(
            return_value={"status": True, "html": server_html or _servers_html()}
        )

        def get_text(url, referer="", params=None):
            if url == embed_url:
                return _embed_page(payload)
            if url == MASTER_URL:
                return MASTER_PLAYLIST
            return ""

        return mock.patch.multiple(
            provider,
            _get_json=get_json,
            _get_text=mock.Mock(side_effect=get_text),
        ), payload

    def test_sub_streams_include_qualities_subtitles_and_referer(self):
        provider = make_provider()
        patcher, _ = self._patch_streams(provider)
        with patcher:
            data = provider.get_streaming_servers("naruto-1335", "1")

        assert data is not None
        qualities = data["Qualities"]
        assert [q.name for q in qualities] == ["1080p", "720p"]
        assert qualities[0].direct_url.endswith("/1080/index.m3u8")
        assert qualities[0].subtitle_url == "https://hls.example/en.vtt"
        assert qualities[0].subtitle_lang == "en"
        # HLS audio is an in-stream rendition, not an external --audio-file.
        assert qualities[0].audio_url == ""
        assert qualities[0].audio_tracks[0]["lang"] == ""
        assert qualities[0].audio_tracks[0]["label"] == "English"
        assert qualities[0].audio_tracks[0]["src"] == "https://hls.example/audio/en.m3u8"
        assert qualities[0].master_url == MASTER_URL
        assert data["AudioTracks"][0]["default"] is True
        assert qualities[0].headers["Referer"] == "https://zokoanime.video/"
        assert qualities[0].audio_lang == "jpn,ja"

    def test_dub_streams_use_dub_hash_and_do_not_attach_subtitles(self):
        provider = make_provider(HiAnimeDubProvider)
        patcher, _ = self._patch_streams(provider, embed_url=DUB_EMBED_URL)
        with patcher:
            data = provider.get_streaming_servers("naruto-1335", "1")

        assert data is not None
        assert data["Mode"] == "dub"
        assert data["Qualities"][0].subtitle_url == ""
        assert data["Qualities"][0].audio_lang == "eng,en"

    def test_missing_dub_server_returns_none_without_language_fallback(self):
        provider = make_provider(HiAnimeDubProvider)
        sub_only = _servers_html().split('data-type="dub"')[0]
        patcher, _ = self._patch_streams(provider, embed_url=DUB_EMBED_URL, server_html=sub_only)
        with patcher:
            data = provider.get_streaming_servers("naruto-1335", "1")

        assert data is None

    def test_playlist_parser_handles_media_playlist_and_audio(self):
        variants, audio = HiAnimeProvider._parse_master(MASTER_URL, MASTER_PLAYLIST)
        assert [(height, url) for height, _bw, url in variants] == [
            (1080, "https://hls.example/1080/index.m3u8"),
            (720, "https://hls.example/720/index.m3u8"),
        ]
        assert audio == "https://hls.example/audio/en.m3u8"

        media = "#EXTM3U\n#EXTINF:10,\nsegment.ts\n"
        assert HiAnimeProvider._parse_master(MASTER_URL, media) == ([], "")


class TestLanguageModes:
    def test_normalization_and_aliases(self):
        assert normalize_language_mode("en_dub") == "en_dub"
        assert normalize_language_mode("English Dub") == "en_dub"
        assert normalize_language_mode("arabic") == "ar_sub"
        assert normalize_language_mode("nonsense") == "ar_sub"
        assert language_mode_label("en_sub") == "English Sub"

    def test_provider_sets_per_mode(self):
        assert "anime3rb" in providers_for_mode("ar_sub")
        assert "stardima" not in providers_for_mode("ar_sub")
        assert providers_for_mode("ar_dub") == ["stardima"]
        assert providers_for_mode("en_sub") == ["hianime"]
        assert providers_for_mode("en_dub") == ["hianime_dub"]
        assert providers_for_mode("cartoon") == ["stardima_kids"]
        assert resolve_provider_id("en_dub", "anime3rb", auto=True) == "hianime_dub"
        assert resolve_provider_id("en_sub", "hianime_dub", auto=False) == "hianime"
        assert resolve_provider_id("ar_dub", "anime3rb", auto=True) == "stardima"
        assert resolve_provider_id("cartoon", "anime3rb", auto=True) == "stardima_kids"

    def test_new_modes_and_labels(self):
        assert normalize_language_mode("Arabic Dub") == "ar_dub"
        assert normalize_language_mode("arabic_dub") == "ar_dub"
        assert normalize_language_mode("Cartoon") == "cartoon"
        assert normalize_language_mode("kids") == "cartoon"
        assert language_mode_label("ar_dub") == "Arabic Dub"
        assert language_mode_label("cartoon") == "Cartoon"
        assert mode_audio_lang("ar_dub") == "ara,ar"
        assert mode_audio_lang("cartoon") == "ara,ar"

    def test_manager_accepts_legacy_hianime_alias(self):
        assert ProviderManager.get_provider("hianime_sub").id == "hianime"
        assert ProviderManager.get_provider("hianime").id == "hianime"
        assert ProviderManager.get_provider("hianime_dub").id == "hianime_dub"

    def test_new_install_defaults_to_arabic_sub(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        settings = SettingsManager()
        assert settings.get("language_mode") == "ar_sub"
        assert settings.get("anime_provider") == "anime3rb"
        assert settings.get("auto_provider") is True

    def test_existing_provider_choice_is_migrated_without_surprise(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        db = tmp_path / ".nyoanime" / "database"
        db.mkdir(parents=True)
        (db / "config.json").write_text(
            json.dumps({"anime_provider": "hianime_dub"}), encoding="utf-8"
        )

        settings = SettingsManager()
        assert settings.get("language_mode") == "en_dub"
        assert settings.get("auto_provider") is False

    @pytest.mark.parametrize(
        "provider_id, expected_mode",
        [
            ("stardima", "ar_dub"),
            ("stardima_anime", "ar_dub"),
            ("stardima_kids", "cartoon"),
            ("stardima_cartoon", "cartoon"),
        ],
    )
    def test_stardima_provider_choice_migrates_to_matching_mode(
        self, tmp_path, monkeypatch, provider_id, expected_mode
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        db = tmp_path / ".nyoanime" / "database"
        db.mkdir(parents=True)
        (db / "config.json").write_text(
            json.dumps({"anime_provider": provider_id}), encoding="utf-8"
        )

        settings = SettingsManager()
        assert settings.get("language_mode") == expected_mode
        assert settings.get("auto_provider") is False

    @pytest.mark.parametrize(
        "provider_id, expected_mode",
        [
            ("stardima", "ar_dub"),
            ("stardima_kids", "cartoon"),
        ],
    )
    def test_existing_stardima_mode_is_reclassified(
        self, tmp_path, monkeypatch, provider_id, expected_mode
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        db = tmp_path / ".nyoanime" / "database"
        db.mkdir(parents=True)
        (db / "config.json").write_text(
            json.dumps(
                {"anime_provider": provider_id, "language_mode": "ar_sub"}
            ),
            encoding="utf-8",
        )

        settings = SettingsManager()
        assert settings.get("language_mode") == expected_mode

    def test_settings_menu_switches_language_and_provider(self, tmp_path, monkeypatch):
        from nyoanime.ui import UIManager

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr("nyoanime.ui.time.sleep", lambda *_args: None)
        settings = SettingsManager()
        ui = UIManager()
        keys = iter(["RIGHT", "RIGHT", "q"])

        with mock.patch("nyoanime.ui.get_key", side_effect=lambda: next(keys)):
            with mock.patch("nyoanime.ui.RawTerminal"):
                ui.settings_menu(settings)

        # ar_sub -> ar_dub -> en_sub
        assert settings.get("language_mode") == "en_sub"
        assert settings.get("anime_provider") == "hianime"

    def test_settings_menu_maps_arabic_dub_and_cartoon(self, tmp_path, monkeypatch):
        from nyoanime.ui import UIManager

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr("nyoanime.ui.time.sleep", lambda *_args: None)
        settings = SettingsManager()
        ui = UIManager()
        keys = iter(["RIGHT", "q"])

        with mock.patch("nyoanime.ui.get_key", side_effect=lambda: next(keys)):
            with mock.patch("nyoanime.ui.RawTerminal"):
                ui.settings_menu(settings)

        assert settings.get("language_mode") == "ar_dub"
        assert settings.get("anime_provider") == "stardima"

    def test_switching_language_resolves_compatible_provider(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        settings = SettingsManager()

        settings.set("language_mode", "en_dub")
        provider_id = ProviderManager.resolve_active_provider(settings, auto=True)
        assert provider_id == "hianime_dub"

        settings.set("language_mode", "en_sub")
        assert ProviderManager.resolve_active_provider(settings, auto=True) == "hianime"

        settings.set("language_mode", "ar_sub")
        assert ProviderManager.resolve_active_provider(settings, auto=True) == "anime3rb"


class TestPlayerSidecarTracks:
    def test_vlc_receives_audio_and_subtitle_slaves(self):
        from nyoanime.player import PlayerManager

        player = PlayerManager(console=None)
        with mock.patch("nyoanime.player.subprocess.run") as run:
            player._play_vlc(
                "https://cdn.example/video.m3u8",
                "Naruto - Ep 1",
                vlc_path="vlc",
                headers={"Referer": "https://zokoanime.video/"},
                subtitle_url="https://cdn.example/en.vtt",
                audio_url="https://cdn.example/en-audio.m3u8",
            )

        args = run.call_args[0][0]
        assert "--input-slave=https://cdn.example/en-audio.m3u8#https://cdn.example/en.vtt" in args
        assert args[-1] == "https://cdn.example/video.m3u8"

    def test_mpv_receives_sidecar_tracks(self):
        from nyoanime import player as player_module

        player = player_module.PlayerManager(console=None)
        with mock.patch.object(player_module, "MpvTracker") as tracker, mock.patch(
            "nyoanime.player.subprocess.run"
        ) as run:
            tracker.return_value.stop.return_value = None
            player._play_mpv(
                "https://cdn.example/video.m3u8",
                "Naruto - Ep 1",
                mpv_path="mpv",
                subtitle_url="https://cdn.example/en.vtt",
                subtitle_lang="en",
                audio_url="https://cdn.example/en-audio.m3u8",
            )

        args = run.call_args[0][0]
        assert "--sub-file=https://cdn.example/en.vtt" in args
        assert "--slang=en,eng,en" in args
        assert "--audio-file=https://cdn.example/en-audio.m3u8" in args
