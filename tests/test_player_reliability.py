from types import SimpleNamespace
from unittest import mock

import pytest

from nyoanime import player as player_module
from nyoanime.models import QualityOption


class FakeTracker:
    def __init__(self, sock_path):
        self.state = {"time_pos": 42.0, "duration": 1440.0, "percent": 3.0, "completed": False}

    def start(self):
        pass

    def stop(self):
        pass


def _patch_terminal(monkeypatch):
    import nyoanime.utils as utils_module

    monkeypatch.setattr(utils_module, "restore_terminal_for_input", lambda: None)
    monkeypatch.setattr(utils_module, "enter_raw_mode_after_input", lambda: None)
    monkeypatch.setattr(utils_module, "show_cursor", lambda: None)
    monkeypatch.setattr(utils_module, "hide_cursor", lambda: None)


def _run_mpv(monkeypatch, **kwargs):
    captured = {}

    def fake_run(args, **run_kwargs):
        captured["args"] = args
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(player_module, "MpvTracker", FakeTracker)
    monkeypatch.setattr(player_module.subprocess, "run", fake_run)
    _patch_terminal(monkeypatch)
    player = player_module.PlayerManager()
    result = player._play_mpv("https://cdn.test/a.m3u8", "title", mpv_path="mpv", **kwargs)
    return captured["args"], result


class TestMpvArguments:
    def test_subtitle_off_disables_all_subtitle_sources(self, monkeypatch):
        args, result = _run_mpv(
            monkeypatch,
            audio_lang="jpn,ja",
            subtitle_url=None,
            subtitles_disabled=True,
        )
        assert "--sid=no" in args
        assert "--sub-auto=no" in args
        assert not any(arg.startswith("--sub-file=") for arg in args)
        assert "--alang=jpn,ja" in args
        assert result["position_reported"] is True

    def test_selected_subtitle_uses_only_its_language(self, monkeypatch):
        args, _ = _run_mpv(
            monkeypatch,
            subtitle_url="https://cdn.test/ar.vtt",
            subtitle_lang="ara",
        )
        assert "--sub-file=https://cdn.test/ar.vtt" in args
        assert "--slang=ara,eng,en" in args

    def test_hls_audio_track_is_not_passed_as_audio_file(self, monkeypatch):
        args, _ = _run_mpv(monkeypatch, audio_lang="eng,en", audio_url=None)
        assert "--alang=eng,en" in args
        assert not any(arg.startswith("--audio-file=") for arg in args)

    def test_master_playback_pins_selected_variant_bitrate(self, monkeypatch):
        args, _ = _run_mpv(monkeypatch, hls_bitrate=2400000)
        assert "--hls-bitrate=2400000" in args

    def test_no_bitrate_flag_without_master_selection(self, monkeypatch):
        args, _ = _run_mpv(monkeypatch)
        assert not any(arg.startswith("--hls-bitrate=") for arg in args)


class TestVlcArguments:
    def _run_vlc(self, **kwargs):
        player = player_module.PlayerManager(console=None)
        with mock.patch("nyoanime.player.subprocess.run") as run:
            result = player._play_vlc(
                "https://cdn.test/a.m3u8",
                "title",
                vlc_path="vlc",
                **kwargs,
            )
        return run.call_args[0][0], result

    def test_vlc_audio_language_uses_single_token(self):
        args, _ = self._run_vlc(audio_lang="eng,en")
        assert "--audio-language=eng" in args
        assert "--audio-language=eng,en" not in args

    def test_vlc_subtitle_disable(self):
        args, result = self._run_vlc(
            subtitle_url="https://cdn.test/en.vtt",
            subtitles_disabled=True,
        )
        assert "--sub-track=-1" in args
        assert "--no-sub-autodetect-file" in args
        assert not any(arg.startswith("--input-slave=") for arg in args)
        assert result["position_reported"] is False
        assert result["time_pos"] == 0.0


class TestNonMpvPositionReporting:
    def test_vlc_does_not_fabricate_resume_position(self):
        player = player_module.PlayerManager(console=None)
        with mock.patch("nyoanime.player.subprocess.run"):
            result = player._play_vlc(
                "https://cdn.test/a.m3u8",
                "title",
                vlc_path="vlc",
                start_time=300,
            )
        assert result["time_pos"] == 0.0
        assert result["position_reported"] is False

    def test_celluloid_and_iina_propagate_mpv_options(self):
        player = player_module.PlayerManager(console=None)
        with mock.patch("nyoanime.player.subprocess.run") as run:
            player._play_celluloid(
                "https://cdn.test/a.m3u8", "title", celluloid_path="celluloid",
                subtitle_url="https://cdn.test/en.vtt", subtitle_lang="en",
                audio_lang="jpn,ja",
            )
            args = run.call_args[0][0]
            assert "--mpv-sub-file=https://cdn.test/en.vtt" in args
            assert "--mpv-slang=en" in args
            assert "--mpv-alang=jpn,ja" in args

        with mock.patch("nyoanime.player.subprocess.run") as run:
            player._play_iina(
                "https://cdn.test/a.m3u8", "title", iina_path="iina-cli",
                subtitles_disabled=True,
            )
            args = run.call_args[0][0]
            assert "--mpv-sid=no" in args
            assert "--mpv-sub-auto=no" in args


class TestTrackMetadata:
    def test_subtitle_union_deduplicates_and_keeps_real_urls(self):
        from nyoanime.app import NyoAnimeApp

        first = QualityOption("1080p", subtitles=[{"lang": "en", "src": "https://s/en.vtt"}])
        second = QualityOption(
            "720p",
            subtitles=[
                {"lang": "en", "src": "https://s/en.vtt"},
                {"lang": "ar", "src": "https://s/ar.vtt"},
                {"lang": "bad", "src": ""},
            ],
        )
        tracks = NyoAnimeApp._collect_subtitles(None, [first, second])
        assert [track["lang"] for track in tracks] == ["en", "ar"]

    def test_audio_track_language_token(self):
        from nyoanime.app import NyoAnimeApp

        assert NyoAnimeApp._audio_lang_for_track({"lang": "eng"}) == "eng"
        assert NyoAnimeApp._audio_lang_for_track({"lang": "pt-BR"}) == "pt-BR,pt"
        assert NyoAnimeApp._audio_lang_for_track({}, "jpn,ja") == "jpn,ja"

    def test_default_audio_index_prefers_language_mode_over_master_default(self):
        from nyoanime.app import NyoAnimeApp

        tracks = [
            {"lang": "eng", "label": "English", "default": True},
            {"lang": "jpn", "label": "Japanese", "default": False},
        ]
        # en_sub must prefer Japanese even if the master marks English default.
        assert NyoAnimeApp._default_audio_index(tracks, "en_sub") == 1
        assert NyoAnimeApp._default_audio_index(tracks, "en_dub") == 0
        assert NyoAnimeApp._default_audio_index([], "en_sub") is None
        # Unknown modes fall back to the master default, then first track.
        assert NyoAnimeApp._default_audio_index(tracks, "xx") == 0
        assert NyoAnimeApp._default_audio_index(
            [{"lang": "jpn", "default": False}, {"lang": "eng", "default": False}], "xx"
        ) == 0

    def test_arabic_mode_selects_arabic_audio(self):
        from nyoanime.app import NyoAnimeApp

        tracks = [{"lang": "jpn", "default": True}, {"lang": "ara", "default": False}]
        assert NyoAnimeApp._default_audio_index(tracks, "ar_sub") == 1

    def test_invalid_subtitle_urls_are_filtered(self, monkeypatch):
        from nyoanime import app as app_module

        calls = []

        def fake_probe(url, headers=None, timeout=8.0):
            calls.append(url)
            return url.endswith("good.vtt"), ""

        monkeypatch.setattr(app_module, "probe_subtitle_url", fake_probe)
        result = app_module.NyoAnimeApp._filter_valid_subtitles(
            None,
            [
                {"lang": "en", "src": "https://s/good.vtt"},
                {"lang": "ar", "src": "https://s/bad.vtt"},
            ],
        )
        assert [track["lang"] for track in result] == ["en"]
        assert len(calls) == 2


class TestHianimeAudioRenditions:
    def test_audio_rendition_parser_reads_master_attributes(self):
        from nyoanime.providers.hianime import HiAnimeProvider

        playlist = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",LANGUAGE="jpn",NAME="Japanese",DEFAULT=YES,URI="audio/jp.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",LANGUAGE="eng",NAME="English",DEFAULT=NO,URI="audio/en.m3u8"
"""
        tracks = HiAnimeProvider._parse_audio_renditions(
            "https://cdn.example/show/master.m3u8", playlist
        )
        assert len(tracks) == 2
        assert tracks[0]["lang"] == "jpn"
        assert tracks[0]["src"] == "https://cdn.example/show/audio/jp.m3u8"
        assert tracks[0]["default"] is True
        assert tracks[1]["label"] == "English"
