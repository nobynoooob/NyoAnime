from pathlib import Path

import pytest
from Crypto.Cipher import AES

from nyoanime.hls import (
    HlsDownloader,
    HlsDrmError,
    HlsError,
    HlsExpiredError,
    HlsCancelledError,
    download_subtitle,
    find_existing_download,
    parse_audio_renditions,
    parse_media_playlist,
    parse_variants,
    select_variant,
)


class FakeResponse:
    def __init__(self, status_code=200, content=b"", text=None):
        self.status_code = status_code
        self.content = content
        self.text = text if text is not None else content.decode("utf-8", "replace")
        self.closed = False

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, routes):
        self.routes = {key: list(value) if isinstance(value, list) else [value] for key, value in routes.items()}
        self.requests = []

    def get(self, url, headers=None, timeout=None):
        self.requests.append((url, dict(headers or {})))
        queue = self.routes.get(url)
        if not queue:
            return FakeResponse(404)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(item):
            return item()
        return item


def _playlist(body):
    return FakeResponse(text="#EXTM3U\n" + body)


def _ts_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    padded = data + bytes([pad]) * pad
    return AES.new(key, AES.MODE_CBC, iv).encrypt(padded)


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720
high/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
fhd/index.m3u8
"""


class TestMasterParsing:
    def test_parses_variants_and_resolves_relative_urls(self):
        variants = parse_variants(MASTER, "https://cdn.example/show/master.m3u8")
        assert [v.height for v in variants] == [360, 720, 1080]
        assert variants[0].url == "https://cdn.example/show/low/index.m3u8"

    def test_selects_closest_at_or_below_requested_quality(self):
        variants = parse_variants(MASTER, "https://cdn.example/master.m3u8")
        assert select_variant(variants, "1080p").height == 1080
        assert select_variant(variants, "720p").height == 720
        assert select_variant(variants, "480p").height == 360
        assert select_variant(variants, "best").height == 1080
        assert select_variant([], "720p") is None

    def test_rejects_invalid_playlist(self):
        with pytest.raises(HlsError):
            parse_variants("not a playlist", "https://cdn.example/x.m3u8")

    def test_audio_renditions_are_parsed_and_selected_by_language(self):
        text = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",LANGUAGE="jpn",NAME="Japanese",DEFAULT=YES,URI="audio/jp.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",LANGUAGE="eng",NAME="English",DEFAULT=NO,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s",LANGUAGE="en",NAME="English",URI="subs/en.m3u8"
"""
        renditions = parse_audio_renditions(text, "https://cdn.example/show/master.m3u8")
        assert [track["lang"] for track in renditions] == ["jpn", "eng"]
        assert renditions[1]["src"] == "https://cdn.example/show/audio/en.m3u8"
        assert HlsDownloader._select_audio(renditions, "eng,en")["lang"] == "eng"
        assert HlsDownloader._select_audio(renditions, "ara")["lang"] == "jpn"
        assert HlsDownloader._select_audio(renditions, "")["lang"] == "jpn"


class TestMediaParsing:
    def test_relative_segments_keys_and_byte_ranges(self):
        body = """#EXTM3U
#EXT-X-VERSION:4
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:7
#EXT-X-KEY:METHOD=AES-128,URI="/keys/k1",IV=0x0000000000000000000000000000000a
#EXT-X-MAP:URI="init.mp4",BYTERANGE="120@0"
#EXTINF:6.0,
seg-1.m4s
#EXT-X-BYTERANGE:200@120
#EXTINF:6.0,
seg-2.m4s
#EXT-X-BYTERANGE:100
#EXTINF:6.0,
seg-2.m4s
#EXT-X-DISCONTINUITY
#EXTINF:4.0,
seg-3.m4s
#EXT-X-ENDLIST
"""
        playlist = parse_media_playlist(body, "https://cdn.example/show/high/index.m3u8")
        assert playlist.ended is True
        assert len(playlist.segments) == 4
        assert playlist.segments[0].url == "https://cdn.example/show/high/seg-1.m4s"
        assert playlist.segments[0].key.uri == "https://cdn.example/keys/k1"
        assert playlist.segments[0].init.url == "https://cdn.example/show/high/init.mp4"
        assert playlist.segments[0].init.byterange == (120, 0)
        assert playlist.segments[1].byterange == (200, 120)
        assert playlist.segments[2].byterange == (100, 320)
        assert playlist.segments[3].discontinuity is True
        assert playlist.duration == pytest.approx(22.0)
        assert playlist.is_fmp4 is True

    def test_empty_media_playlist_is_rejected(self):
        with pytest.raises(HlsError):
            parse_media_playlist("#EXTM3U\n#EXT-X-ENDLIST\n", "https://cdn.example/x.m3u8")


class TestDownload:
    def _media(self):
        return _playlist(
            "#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg1.ts\n#EXTINF:4.0,\nseg2.ts\n#EXT-X-ENDLIST\n"
        )

    def test_downloads_segments_and_reports_progress(self, tmp_path):
        session = FakeSession(
            {
                "https://cdn.example/media.m3u8": self._media(),
                "https://cdn.example/seg1.ts": FakeResponse(content=b"AAA"),
                "https://cdn.example/seg2.ts": FakeResponse(content=b"BBB"),
            }
        )
        events = []
        downloader = HlsDownloader(
            headers={"Referer": "https://site.example/"},
            session=session,
            allow_remux=False,
        )
        result = downloader.download(
            "https://cdn.example/media.m3u8",
            tmp_path / "Episode.mp4",
            progress=lambda done, total, written, size, quality: events.append((done, total)),
        )

        assert result.path.suffix == ".ts"
        assert result.path.read_bytes() == b"AAABBB"
        assert events[-1] == (2, 2)
        # Headers reached every segment request, not just the playlist.
        assert all(request[1]["Referer"] == "https://site.example/" for request in session.requests)

    def test_aes128_and_init_section_are_decrypted_and_concatenated(self, tmp_path):
        key = b"0123456789abcdef"
        iv1 = (1).to_bytes(16, "big")
        iv2 = (2).to_bytes(16, "big")
        body = """#EXT-X-VERSION:7
#EXT-X-MEDIA-SEQUENCE:1
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.0,
one.m4s
#EXTINF:4.0,
two.m4s
#EXT-X-ENDLIST
"""
        session = FakeSession(
            {
                "https://cdn.example/show/index.m3u8": _playlist(body),
                "https://cdn.example/show/init.mp4": FakeResponse(content=b"INIT"),
                "https://cdn.example/show/key.bin": FakeResponse(content=key),
                "https://cdn.example/show/one.m4s": FakeResponse(content=_ts_encrypt(b"ONE!", key, iv1)),
                "https://cdn.example/show/two.m4s": FakeResponse(content=_ts_encrypt(b"TWO!", key, iv2)),
            }
        )
        result = HlsDownloader(session=session, allow_remux=False).download(
            "https://cdn.example/show/index.m3u8", tmp_path / "ep.mp4"
        )

        assert result.path.read_bytes() == b"INITONE!TWO!"
        assert result.container == "mp4"

    def test_sample_aes_drm_is_rejected_without_writing(self, tmp_path):
        body = """#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key"
#EXTINF:4.0,
one.ts
#EXT-X-ENDLIST
"""
        dest = tmp_path / "ep.mp4"
        session = FakeSession({"https://cdn.example/index.m3u8": _playlist(body)})
        with pytest.raises(HlsDrmError):
            HlsDownloader(session=session).download("https://cdn.example/index.m3u8", dest)
        assert not dest.exists()
        assert not Path(str(dest) + ".part").exists()

    def test_expired_stream_raises(self, tmp_path):
        session = FakeSession({"https://cdn.example/index.m3u8": FakeResponse(403)})
        with pytest.raises(HlsExpiredError):
            HlsDownloader(session=session, max_retries=0).download(
                "https://cdn.example/index.m3u8", tmp_path / "ep.mp4"
            )

    def test_transient_failure_is_retried(self, tmp_path, monkeypatch):
        session = FakeSession(
            {
                "https://cdn.example/media.m3u8": [
                    FakeResponse(500),
                    self._media(),
                ],
                "https://cdn.example/seg1.ts": FakeResponse(content=b"A"),
                "https://cdn.example/seg2.ts": FakeResponse(content=b"B"),
            }
        )
        downloader = HlsDownloader(session=session, max_retries=2, allow_remux=False)
        monkeypatch.setattr(downloader, "_sleep", lambda attempt: None)
        result = downloader.download("https://cdn.example/media.m3u8", tmp_path / "ep.mp4")
        assert result.path.read_bytes() == b"AB"

    def test_cancel_keeps_part_file_and_retry_resumes(self, tmp_path, monkeypatch):
        session = FakeSession(
            {
                "https://cdn.example/media.m3u8": self._media(),
                "https://cdn.example/seg1.ts": FakeResponse(content=b"AAA"),
                "https://cdn.example/seg2.ts": FakeResponse(content=b"BBB"),
            }
        )
        downloader = HlsDownloader(session=session, allow_remux=False)
        observed = []

        def progress(done, total, written, size, quality):
            observed.append(done)
            downloader.stop_requested.set()

        with pytest.raises(HlsCancelledError):
            downloader.download("https://cdn.example/media.m3u8", tmp_path / "ep.mp4", progress=progress)

        part = tmp_path / "ep.ts.part"
        assert part.exists()
        assert part.read_bytes() == b"AAA"
        assert Path(str(part) + ".json").exists()

        retry = HlsDownloader(session=session, allow_remux=False)
        request_count = len(session.requests)
        result = retry.download("https://cdn.example/media.m3u8", tmp_path / "ep.mp4")
        assert result.path.read_bytes() == b"AAABBB"
        # Only the second segment is fetched on resume.
        assert sum(1 for url, _ in session.requests[request_count:] if url.endswith("seg1.ts")) == 0

    def test_live_playlist_without_endlist_is_refused(self, tmp_path):
        body = "#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg1.ts\n"
        session = FakeSession(
            {
                "https://cdn.example/live.m3u8": _playlist(body),
                "https://cdn.example/seg1.ts": FakeResponse(content=b"A"),
            }
        )
        with pytest.raises(HlsError, match="Live HLS"):
            HlsDownloader(session=session).download("https://cdn.example/live.m3u8", tmp_path / "ep.mp4")

    def test_master_audio_rendition_is_downloaded_and_muxed(self, tmp_path, monkeypatch):
        master = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",LANGUAGE="eng",NAME="English",DEFAULT=YES,URI="audio/en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720,AUDIO="aud"
video/720.m3u8
"""
        video_media = _playlist("#EXTINF:4.0,\nv1.ts\n#EXT-X-ENDLIST\n")
        audio_media = _playlist("#EXTINF:4.0,\na1.ts\n#EXT-X-ENDLIST\n")
        session = FakeSession(
            {
                "https://cdn.example/master.m3u8": FakeResponse(text=master),
                "https://cdn.example/video/720.m3u8": video_media,
                "https://cdn.example/video/v1.ts": FakeResponse(content=b"VID"),
                "https://cdn.example/audio/en.m3u8": audio_media,
                "https://cdn.example/audio/a1.ts": FakeResponse(content=b"AUD"),
            }
        )
        seen = {}

        def fake_mux(video_path, audio_path, dest_path):
            seen["video"] = video_path.read_bytes()
            seen["audio"] = audio_path.read_bytes()
            output = dest_path.with_suffix(".muxed.mp4")
            output.write_bytes(b"muxed")
            return output

        downloader = HlsDownloader(session=session, allow_remux=False)
        monkeypatch.setattr(downloader, "_mux_av", fake_mux)
        result = downloader.download(
            "https://cdn.example/master.m3u8",
            tmp_path / "ep.mp4",
            preferred_quality="720p",
            audio_lang="eng,en",
        )

        assert seen == {"video": b"VID", "audio": b"AUD"}
        assert result.path == tmp_path / "ep.mp4"
        assert result.path.read_bytes() == b"muxed"
        assert not (tmp_path / "ep.audio.ts").exists()
        assert not (tmp_path / "ep.ts.part").exists()


class TestDownloadHelpers:
    def test_find_existing_download_ignores_partial_files(self, tmp_path):
        target = tmp_path / "Show - Ep 1 [1080p].mp4"
        target.write_bytes(b"video")
        assert find_existing_download(target) == target
        Path(str(target) + ".part").write_bytes(b"x")
        assert find_existing_download(target) is None

    def test_subtitle_sidecar_accepts_vtt_and_rejects_html(self, tmp_path, monkeypatch):
        import nyoanime.hls as hls_module

        class TextResponse:
            text = "WEBVTT\n\n00:00.000 --> 00:01.000\nHi\n"

            def raise_for_status(self):
                return None

        monkeypatch.setattr(hls_module.requests, "get", lambda *a, **k: TextResponse())
        video = tmp_path / "Show - Ep 1.mp4"
        video.write_bytes(b"v")
        saved = download_subtitle("https://cdn.example/en.vtt", video, "en")
        assert saved == tmp_path / "Show - Ep 1.en.vtt"
        assert saved.read_text().startswith("WEBVTT")

        class HtmlResponse(TextResponse):
            text = "<html>nope</html>"

        monkeypatch.setattr(hls_module.requests, "get", lambda *a, **k: HtmlResponse())
        assert download_subtitle("https://cdn.example/bad.vtt", video, "en") is None
