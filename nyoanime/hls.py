"""
Native HLS (VOD) downloader for NyoAnime.

Supports master/media playlists, quality variants, fMP4 init sections,
AES-128 segment encryption, byte ranges, discontinuities, resumable
downloads, cancellation and subtitle sidecars.

Deliberately does not handle SAMPLE-AES, Widevine, FairPlay or any other
DRM/access-control scheme.  Such playlists are rejected instead of being
saved as broken files.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

from .logger import logger

ProgressCallback = Callable[[int, int, int, Optional[int], str], None]

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class HlsError(Exception):
    """Base HLS failure."""


class HlsExpiredError(HlsError):
    """The signed stream or a segment is no longer accessible."""


class HlsDrmError(HlsError):
    """The playlist requires an unsupported DRM scheme."""


class HlsCancelledError(HlsError):
    """Download was cancelled by the user."""


class HlsDiskSpaceError(HlsError):
    """Not enough free space for the estimated download size."""


@dataclass
class HlsKey:
    method: str
    uri: str = ""
    iv: bytes = b""


@dataclass
class HlsInit:
    url: str
    byterange: Optional[Tuple[int, int]] = None


@dataclass
class HlsSegment:
    url: str
    duration: float = 0.0
    sequence: int = 0
    byterange: Optional[Tuple[int, int]] = None
    key: Optional[HlsKey] = None
    init: Optional[HlsInit] = None
    discontinuity: bool = False


@dataclass
class HlsVariant:
    url: str
    bandwidth: int = 0
    width: int = 0
    height: int = 0
    codecs: str = ""


@dataclass
class HlsMediaPlaylist:
    url: str
    segments: List[HlsSegment] = field(default_factory=list)
    duration: float = 0.0
    ended: bool = False

    @property
    def is_fmp4(self) -> bool:
        return any(segment.init for segment in self.segments)


@dataclass
class HlsDownloadResult:
    path: Path
    quality: str
    segments: int
    bytes_written: int
    container: str


def _parse_attrs(payload: str) -> Dict[str, str]:
    """Parse an EXT-X attribute list, respecting quoted commas."""
    parts: List[str] = []
    buf: List[str] = []
    quoted = False
    for char in payload or "":
        if char == '"':
            quoted = not quoted
            buf.append(char)
        elif char == "," and not quoted:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(char)
    if buf:
        parts.append("".join(buf).strip())

    attrs: Dict[str, str] = {}
    for part in parts:
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        attrs[name.strip().upper()] = value
    return attrs


def _parse_byterange(value: str) -> Optional[Tuple[int, Optional[int]]]:
    match = re.match(r"^\s*(\d+)(?:@(\d+))?\s*$", value or "")
    if not match:
        return None
    length = int(match.group(1))
    offset = int(match.group(2)) if match.group(2) is not None else None
    return length, offset


def _is_playlist(text: str) -> bool:
    return "#EXTM3U" in (text or "")[:512]


def parse_audio_renditions(text: str, base_url: str) -> List[Dict[str, Any]]:
    """Return EXT-X-MEDIA audio renditions from an HLS master playlist."""
    renditions: List[Dict[str, Any]] = []
    seen = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = _parse_attrs(line.split(":", 1)[1])
        if (attrs.get("TYPE") or "").upper() != "AUDIO":
            continue
        uri = (attrs.get("URI") or "").strip()
        if not uri:
            continue
        lang = (attrs.get("LANGUAGE") or "").strip()
        label = (attrs.get("NAME") or "").strip() or lang
        key = (lang, label, uri)
        if key in seen:
            continue
        seen.add(key)
        renditions.append(
            {
                "lang": lang,
                "label": label,
                "src": urljoin(base_url, uri),
                "default": (attrs.get("DEFAULT") or "").upper() == "YES",
            }
        )
    return renditions


def parse_variants(text: str, base_url: str) -> List[HlsVariant]:
    """Extract variant streams from an HLS master playlist."""
    if not _is_playlist(text):
        raise HlsError("Invalid HLS playlist (missing #EXTM3U).")
    variants: List[HlsVariant] = []
    pending: Optional[Dict[str, str]] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = _parse_attrs(line.split(":", 1)[1])
            continue
        if line.startswith("#"):
            continue
        if pending is not None:
            resolution = pending.get("RESOLUTION", "")
            width = height = 0
            match = re.match(r"(\d+)x(\d+)", resolution)
            if match:
                width, height = int(match.group(1)), int(match.group(2))
            try:
                bandwidth = int(pending.get("BANDWIDTH") or 0)
            except ValueError:
                bandwidth = 0
            variants.append(
                HlsVariant(
                    url=urljoin(base_url, line),
                    bandwidth=bandwidth,
                    width=width,
                    height=height,
                    codecs=pending.get("CODECS", ""),
                )
            )
            pending = None
    return variants


def select_variant(variants: List[HlsVariant], preferred_quality: str = "1080p") -> Optional[HlsVariant]:
    """Choose the closest variant at or below the requested height."""
    if not variants:
        return None
    match = re.search(r"(\d{3,4})", preferred_quality or "")
    preferred = int(match.group(1)) if match else 0
    with_height = [variant for variant in variants if variant.height]
    if with_height:
        ordered = sorted(with_height, key=lambda v: (v.height, v.bandwidth), reverse=True)
        if preferred:
            eligible = [variant for variant in ordered if variant.height <= preferred]
            return eligible[0] if eligible else ordered[-1]
        return ordered[0]
    return max(variants, key=lambda variant: variant.bandwidth or 0)


def parse_media_playlist(text: str, base_url: str) -> HlsMediaPlaylist:
    """Parse a media playlist into segments, keys and init sections."""
    if not _is_playlist(text):
        raise HlsError("Invalid HLS playlist (missing #EXTM3U).")

    playlist = HlsMediaPlaylist(url=base_url)
    sequence = 0
    duration: Optional[float] = None
    byterange: Optional[Tuple[int, Optional[int]]] = None
    current_key: Optional[HlsKey] = None
    current_init: Optional[HlsInit] = None
    discontinuity = False
    ranges: Dict[str, int] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1].strip())
            except ValueError:
                sequence = 0
            continue
        if line.startswith("#EXT-X-KEY:"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            method = (attrs.get("METHOD") or "NONE").upper()
            if method == "NONE":
                current_key = None
            elif method == "AES-128":
                key_uri = attrs.get("URI", "")
                if not key_uri:
                    raise HlsError("AES-128 key is missing a URI.")
                iv = b""
                raw_iv = attrs.get("IV", "")
                if raw_iv.lower().startswith("0x"):
                    try:
                        iv = bytes.fromhex(raw_iv[2:].zfill(32))
                    except ValueError as error:
                        raise HlsError("Invalid AES-128 IV.") from error
                current_key = HlsKey(
                    method=method,
                    uri=urljoin(base_url, key_uri),
                    iv=iv,
                )
            elif method in ("SAMPLE-AES", "SAMPLE-AES-CTR", "SAMPLE-AES-CENC"):
                raise HlsDrmError(
                    "This stream is DRM-protected (SAMPLE-AES); downloading is unsupported."
                )
            else:
                raise HlsDrmError(f"Unsupported HLS encryption method: {method}")
            continue
        if line.startswith("#EXT-X-MAP:"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            uri = attrs.get("URI", "")
            if uri:
                br = _parse_byterange(attrs.get("BYTERANGE", ""))
                current_init = HlsInit(
                    url=urljoin(base_url, uri),
                    byterange=(br[0], br[1] or 0) if br else None,
                )
            continue
        if line.startswith("#EXT-X-BYTERANGE:"):
            byterange = _parse_byterange(line.split(":", 1)[1])
            continue
        if line == "#EXT-X-DISCONTINUITY":
            discontinuity = True
            continue
        if line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",", 1)[0].strip())
            except ValueError:
                duration = 0.0
            continue
        if line.startswith("#EXT-X-ENDLIST"):
            playlist.ended = True
            continue
        if line.startswith("#"):
            continue

        url = urljoin(base_url, line)
        resolved_range: Optional[Tuple[int, int]] = None
        if byterange:
            length, offset = byterange
            if offset is None:
                offset = ranges.get(url, 0)
            resolved_range = (length, offset)
            ranges[url] = offset + length

        seg_duration = duration or 0.0
        playlist.segments.append(
            HlsSegment(
                url=url,
                duration=seg_duration,
                sequence=sequence,
                byterange=resolved_range,
                key=current_key,
                init=current_init,
                discontinuity=discontinuity,
            )
        )
        playlist.duration += seg_duration
        sequence += 1
        duration = None
        byterange = None
        discontinuity = False

    if not playlist.segments:
        raise HlsError("HLS media playlist contains no segments.")
    return playlist


def _fingerprint(playlist: HlsMediaPlaylist, quality: str) -> str:
    digest = hashlib.sha256()
    digest.update((quality or "").encode())
    digest.update(b"\n")
    for segment in playlist.segments:
        digest.update(segment.url.encode())
        digest.update(b"|")
        if segment.init:
            digest.update(segment.init.url.encode())
        digest.update(b"|")
        if segment.key:
            digest.update(segment.key.uri.encode())
        digest.update(b"|")
        if segment.byterange:
            digest.update(f"{segment.byterange[0]}@{segment.byterange[1]}".encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _estimate_bytes(variant: Optional[HlsVariant], playlist: HlsMediaPlaylist) -> Optional[int]:
    if not variant or not variant.bandwidth or playlist.duration <= 0:
        return None
    return int(variant.bandwidth / 8 * playlist.duration)


def _strip_padding(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad:
        tail = data[-pad:]
        if all(byte == pad for byte in tail):
            return data[:-pad]
    return data


class HlsDownloader:
    """Download an HLS VOD stream into a playable local file."""

    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        speed_limit_bytes: Optional[int] = None,
        max_retries: int = 3,
        timeout: Tuple[float, float] = (8.0, 30.0),
        allow_remux: bool = True,
        session: Optional[requests.Session] = None,
        stop_event: Optional[threading.Event] = None,
    ):
        self.headers = {"User-Agent": _DEFAULT_UA}
        for name, value in (headers or {}).items():
            if value:
                self.headers[str(name)] = str(value)
        self.speed_limit = speed_limit_bytes
        self.max_retries = max(0, int(max_retries))
        self.timeout = timeout
        self.allow_remux = allow_remux
        self.session = session or requests.Session()
        self.stop_requested = stop_event or threading.Event()
        self._key_cache: Dict[str, bytes] = {}

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _sleep(self, attempt: int) -> None:
        time.sleep(min(4.0, 0.4 * (2 ** attempt)))

    def _request(self, url: str, byterange: Optional[Tuple[int, int]] = None, as_text: bool = False):
        last_error: Optional[Exception] = None
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            if self.stop_requested.is_set():
                raise HlsCancelledError("Download cancelled.")
            headers = dict(self.headers)
            if byterange:
                length, start = byterange
                headers["Range"] = f"bytes={start}-{start + length - 1}"
            try:
                response = self.session.get(url, headers=headers, timeout=self.timeout)
            except requests.RequestException as error:
                last_error = error
                if attempt + 1 < attempts:
                    self._sleep(attempt)
                    continue
                raise HlsError(f"Network error while fetching {urlparse(url).netloc}: {error}") from error

            try:
                status = response.status_code
                if status in (401, 403, 404, 410):
                    raise HlsExpiredError(
                        f"Stream returned HTTP {status}; the link has likely expired."
                    )
                if status in (408, 425, 429) or status >= 500:
                    last_error = HlsError(f"HTTP {status} from stream host.")
                    if attempt + 1 < attempts:
                        self._sleep(attempt)
                        continue
                    raise last_error
                if status not in (200, 206):
                    raise HlsError(f"Unexpected HTTP {status} from stream host.")
                if as_text:
                    return response.text
                data = response.content or b""
                if byterange and len(data) != byterange[0]:
                    raise HlsError(
                        f"Short segment read ({len(data)} of {byterange[0]} bytes)."
                    )
                return data
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        raise last_error or HlsError("Unable to fetch HLS resource.")

    def _fetch_text(self, url: str) -> str:
        return self._request(url, as_text=True)

    def _fetch_key(self, key: HlsKey) -> bytes:
        cached = self._key_cache.get(key.uri)
        if cached is not None:
            return cached
        data = self._request(key.uri)
        if len(data) != 16:
            raise HlsError("AES-128 key has an invalid length.")
        self._key_cache[key.uri] = data
        return data

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def resolve_playlist(self, url: str, preferred_quality: str = "1080p"):
        """Return (media playlist, selected variant, audio renditions)."""
        text = self._fetch_text(url)
        self._reject_drm(text)
        variants = parse_variants(text, url) if "#EXT-X-STREAM-INF" in text else []
        audio_renditions = parse_audio_renditions(text, url) if "#EXT-X-MEDIA" in text else []
        variant = select_variant(variants, preferred_quality) if variants else None
        media_url = variant.url if variant else url
        if variant:
            text = self._fetch_text(media_url)
            self._reject_drm(text)
        if "#EXT-X-STREAM-INF" in text:
            raise HlsError("Nested HLS master playlists are unsupported.")
        playlist = parse_media_playlist(text, media_url)
        return playlist, variant, audio_renditions

    @staticmethod
    def _reject_drm(text: str) -> None:
        upper = (text or "").upper()
        if "SAMPLE-AES" in upper or "WIDEVINE" in upper or "FAIRPLAY" in upper:
            raise HlsDrmError(
                "This stream is DRM-protected; downloading is unsupported."
            )

    def download(
        self,
        url: str,
        dest_path: Path,
        preferred_quality: str = "1080p",
        progress: Optional[ProgressCallback] = None,
        allow_remux: Optional[bool] = None,
        audio_lang: str = "",
        include_audio: bool = True,
    ) -> HlsDownloadResult:
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        playlist, variant, audio_renditions = self.resolve_playlist(url, preferred_quality)
        if not playlist.ended:
            raise HlsError("Live HLS playlists cannot be downloaded (no #EXT-X-ENDLIST).")

        audio_track = (
            self._select_audio(audio_renditions, audio_lang)
            if include_audio and audio_renditions
            else None
        )

        container = "mp4" if playlist.is_fmp4 else "ts"
        if dest_path.suffix.lower() not in (".mp4", ".ts", ".mkv"):
            dest_path = dest_path.with_suffix(f".{container}")
        elif dest_path.suffix.lower() == ".mp4" and container == "ts":
            dest_path = dest_path.with_suffix(".ts")

        quality = (
            f"{variant.height}p"
            if variant and variant.height
            else (dest_path.stem.rsplit("[", 1)[-1].rstrip("]") if "[" in dest_path.stem else "HLS")
        )
        fingerprint = _fingerprint(playlist, quality)
        part_path = dest_path.with_suffix(dest_path.suffix + ".part")
        meta_path = Path(str(part_path) + ".json")

        total_estimate = _estimate_bytes(variant, playlist)
        self._check_disk_space(dest_path.parent, total_estimate)

        start_index, written = self._resume_state(part_path, meta_path, fingerprint, playlist)
        mode = "ab" if start_index > 0 and part_path.exists() else "wb"
        if mode == "wb" and part_path.exists():
            part_path.unlink(missing_ok=True)

        total_segments = len(playlist.segments)

        try:
            with open(part_path, mode) as handle:
                for index in range(start_index, total_segments):
                    if self.stop_requested.is_set():
                        self._write_meta(meta_path, fingerprint, index, written, quality)
                        raise HlsCancelledError("Download cancelled.")

                    segment = playlist.segments[index]
                    previous_init = playlist.segments[index - 1].init if index > 0 else None
                    if segment.init and (index == 0 or segment.init != previous_init):
                        init_data = self._request(segment.init.url, segment.init.byterange)
                        handle.write(init_data)
                        written += len(init_data)

                    data = self._request(segment.url, segment.byterange)
                    if segment.key:
                        data = self._decrypt(data, segment)

                    handle.write(data)
                    written += len(data)
                    self._write_meta(meta_path, fingerprint, index + 1, written, quality)

                    if progress:
                        progress(index + 1, total_segments, written, total_estimate, quality)
                    if self.speed_limit and self.speed_limit > 0:
                        time.sleep(min(0.5, len(data) / self.speed_limit))
        except HlsCancelledError:
            # Keep the .part and metadata so the next run can resume.
            raise
        except (HlsExpiredError, HlsDrmError):
            # A signed out/DRM stream can never be resumed; drop the stale data.
            self._cleanup(part_path, meta_path)
            raise

        if written <= 0 or not part_path.exists() or part_path.stat().st_size != written:
            self._cleanup(part_path, meta_path)
            raise HlsError("Incomplete download discarded; no valid file was produced.")

        final_path = part_path
        effective_remux = self.allow_remux if allow_remux is None else allow_remux
        if audio_track:
            # Separate HLS audio rendition: download it and mux with ffmpeg.
            audio_base = dest_path.with_suffix(".audio.ts")
            audio_result = self.download(
                audio_track["src"],
                audio_base,
                preferred_quality="best",
                progress=None,
                allow_remux=False,
                include_audio=False,
            )
            try:
                muxed = self._mux_av(part_path, audio_result.path, dest_path)
            finally:
                audio_result.path.unlink(missing_ok=True)
                Path(str(audio_result.path) + ".part").unlink(missing_ok=True)
                Path(str(audio_result.path) + ".part.json").unlink(missing_ok=True)
            if muxed:
                part_path.unlink(missing_ok=True)
                final_path = muxed
                container = "mp4"
                dest_path = dest_path.with_suffix(".mp4")
            else:
                self._cleanup(part_path, meta_path)
                raise HlsError(
                    "Stream has a separate audio rendition but ffmpeg muxing failed; "
                    "install ffmpeg to download this episode."
                )
        elif container == "ts" and effective_remux:
            remuxed = self._try_remux(part_path)
            if remuxed:
                final_path = remuxed
                container = "mp4"
                dest_path = dest_path.with_suffix(".mp4")

        final_path.replace(dest_path)
        meta_path.unlink(missing_ok=True)
        if progress:
            progress(total_segments, total_segments, written, total_estimate, quality)

        logger.debug(
            "HLS",
            f"Downloaded {dest_path.name}: {total_segments} segments, {written} bytes",
        )
        return HlsDownloadResult(
            path=dest_path,
            quality=quality,
            segments=total_segments,
            bytes_written=written,
            container=container,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _check_disk_space(directory: Path, estimated: Optional[int]) -> None:
        if not estimated or estimated <= 0:
            return
        try:
            free = shutil.disk_usage(directory).free
        except OSError:
            return
        required = int(estimated * 1.15) + (16 * 1024 * 1024)
        if required > free:
            raise HlsDiskSpaceError(
                f"Not enough disk space: need about {required // (1024*1024)} MB, "
                f"only {free // (1024*1024)} MB free."
            )

    @staticmethod
    def _write_meta(meta_path: Path, fingerprint: str, segments: int, written: int, quality: str) -> None:
        payload = {
            "fingerprint": fingerprint,
            "segments": int(segments),
            "bytes": int(written),
            "quality": quality,
        }
        temp = Path(str(meta_path) + ".tmp")
        try:
            temp.write_text(json.dumps(payload), encoding="utf-8")
            temp.replace(meta_path)
        except OSError:
            pass

    def _resume_state(
        self,
        part_path: Path,
        meta_path: Path,
        fingerprint: str,
        playlist: HlsMediaPlaylist,
    ) -> Tuple[int, int]:
        if not part_path.exists() or not meta_path.exists():
            return 0, 0
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            self._cleanup(part_path, meta_path)
            return 0, 0

        if meta.get("fingerprint") != fingerprint:
            self._cleanup(part_path, meta_path)
            return 0, 0

        segments = int(meta.get("segments") or 0)
        written = int(meta.get("bytes") or 0)
        size = part_path.stat().st_size
        if segments <= 0 or segments > len(playlist.segments) or written <= 0 or size != written:
            self._cleanup(part_path, meta_path)
            return 0, 0
        return segments, written

    def _decrypt(self, data: bytes, segment: HlsSegment) -> bytes:
        from Crypto.Cipher import AES

        key = segment.key
        if key is None:
            return data
        key_bytes = self._fetch_key(key)
        iv = key.iv if len(key.iv) == 16 else int(segment.sequence).to_bytes(16, "big")
        try:
            clear = AES.new(key_bytes, AES.MODE_CBC, iv).decrypt(data)
        except Exception as error:
            raise HlsError(f"AES-128 decryption failed: {error}") from error
        return _strip_padding(clear)

    @staticmethod
    def _select_audio(renditions: List[Dict[str, Any]], audio_lang: str = "") -> Optional[Dict[str, Any]]:
        if not renditions:
            return None
        tokens = [token.strip().lower() for token in str(audio_lang or "").split(",") if token.strip()]
        for track in renditions:
            lang = str(track.get("lang") or "").lower()
            label = str(track.get("label") or "").lower()
            if lang and any(lang == token or lang.startswith(token) or token.startswith(lang) for token in tokens):
                return track
            if label and any(token in label for token in tokens):
                return track
        for track in renditions:
            if track.get("default"):
                return track
        return renditions[0]

    def _mux_av(self, video_path: Path, audio_path: Path, dest_path: Path) -> Optional[Path]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        output = dest_path.with_suffix(".muxed.mp4")
        command = [
            ffmpeg,
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1800,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            logger.debug("HLS", f"Audio mux failed: {error}")
            output.unlink(missing_ok=True)
            return None
        if result.returncode != 0 or not output.exists() or output.stat().st_size <= 0:
            logger.debug("HLS", "ffmpeg audio mux produced no usable output.")
            output.unlink(missing_ok=True)
            return None
        return output

    def _try_remux(self, part_path: Path) -> Optional[Path]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        output = part_path.with_suffix(".remux.mp4")
        command = [
            ffmpeg,
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(part_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1800,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            logger.debug("HLS", f"Remux failed: {error}")
            output.unlink(missing_ok=True)
            return None
        if result.returncode != 0 or not output.exists() or output.stat().st_size <= 0:
            logger.debug("HLS", "Remux produced no usable output; keeping TS.")
            output.unlink(missing_ok=True)
            return None
        part_path.unlink(missing_ok=True)
        return output

    @staticmethod
    def _cleanup(part_path: Path, meta_path: Path) -> None:
        part_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)


def download_subtitle(
    url: str,
    video_path: Path,
    lang: str = "",
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20.0,
) -> Optional[Path]:
    """Save a remote subtitle next to a video, validating that it is text-based."""
    if not url or not str(url).startswith("http"):
        return None
    request_headers = {"User-Agent": _DEFAULT_UA}
    for name, value in (headers or {}).items():
        if value:
            request_headers[str(name)] = str(value)

    try:
        response = requests.get(url, headers=request_headers, timeout=timeout)
        response.raise_for_status()
        content = response.text or ""
    except Exception as error:
        logger.debug("HLS", f"Subtitle download failed: {error}")
        return None

    head = content.lstrip("\ufeff \r\n\t")[:64].upper()
    if "WEBVTT" not in head and "-->" not in content[:4096]:
        logger.debug("HLS", "Subtitle payload is not WEBVTT/SRT; skipped.")
        return None

    suffix = ".srt" if str(url).lower().split("?", 1)[0].endswith(".srt") else ".vtt"
    safe_lang = re.sub(r"[^A-Za-z0-9_-]+", "", lang or "") or "sub"
    dest = Path(video_path).with_suffix(f".{safe_lang}{suffix}")
    temp = Path(str(dest) + ".part")
    try:
        temp.write_text(content, encoding="utf-8")
        temp.replace(dest)
    except OSError as error:
        logger.debug("HLS", f"Subtitle save failed: {error}")
        temp.unlink(missing_ok=True)
        return None
    return dest


def probe_subtitle_url(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 8.0,
) -> Tuple[bool, str]:
    """Lightweight check that a subtitle URL is reachable and textual."""
    if not url or not str(url).startswith("http"):
        return False, "missing URL"
    request_headers = {"User-Agent": _DEFAULT_UA}
    for name, value in (headers or {}).items():
        if value:
            request_headers[str(name)] = str(value)
    try:
        response = requests.get(url, headers=request_headers, timeout=timeout, stream=True)
        try:
            if response.status_code in (401, 403, 404, 410):
                return False, f"HTTP {response.status_code}"
            if response.status_code not in (200, 206):
                return False, f"HTTP {response.status_code}"
            sample = b""
            for chunk in response.iter_content(chunk_size=512):
                sample += chunk
                if len(sample) >= 1024:
                    break
        finally:
            response.close()
    except requests.RequestException as error:
        return False, str(error)

    text = sample.decode("utf-8", errors="replace").lstrip("\ufeff \r\n\t")
    if "WEBVTT" in text[:64].upper() or "-->" in text:
        return True, ""
    return False, "not a subtitle file"


def find_existing_download(dest_path: Path) -> Optional[Path]:
    """Return an existing completed file for a download target, if any."""
    dest_path = Path(dest_path)
    candidates = [dest_path]
    for suffix in (".mp4", ".ts", ".mkv"):
        candidates.append(dest_path.with_suffix(suffix))
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        part = Path(str(candidate) + ".part")
        if candidate.exists() and candidate.stat().st_size > 0 and not part.exists():
            return candidate
    return None
