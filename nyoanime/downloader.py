"""
Native Downloader for NyoAnime.
Provides resumable chunked downloads, speed throttling, concurrent workers,
and interactive batch link resolution without external tools (aria2/IDM).
"""

import re
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import threading

import requests
from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn
)

from .models import Episode
from .utils import get_stream_referer
from .logger import logger
from .hls import (
    HlsCancelledError,
    HlsDiskSpaceError,
    HlsDrmError,
    HlsDownloader,
    HlsError,
    HlsExpiredError,
    download_subtitle,
    find_existing_download,
)


@dataclass
class DownloadConfig:
    save_dir: Path
    max_parallel: int = 2
    speed_limit_bytes: Optional[int] = None
    quality: str = "1080p"
    subtitle_lang: Optional[str] = None
    provider: str = ""
    language_mode: str = ""


@dataclass
class DownloadPlan:
    url: str
    headers: Dict[str, str]
    quality: str
    ext: str = "mp4"
    subtitle_url: str = ""
    subtitle_lang: str = ""
    subtitles: List[Dict[str, Any]] = field(default_factory=list)
    master_url: str = ""
    audio_lang: str = ""

    @property
    def is_hls(self) -> bool:
        # A provider-declared master playlist is authoritative even when the
        # URL does not carry an .m3u8 suffix (some signed CDNs omit it).
        return bool(self.master_url) or is_hls_url(self.url)


def parse_batch_ranges(prompt: str, max_eps: Optional[int] = None) -> List[int]:
    """Parse episode range expressions like '1-12', '1, 3, 5', '5-1', 'all' into sorted episode numbers."""
    if not prompt:
        return []
    p = prompt.strip().lower()
    if p in ("all", "*"):
        if max_eps and max_eps > 0:
            return list(range(1, max_eps + 1))
        return []

    tokens = re.split(r"[\s,;]+", p)
    nums = set()

    for token in tokens:
        if not token:
            continue
        range_match = re.match(r"^(\d+)(?:-|\.\.)(\d+)$", token)
        if range_match:
            s, e = int(range_match.group(1)), int(range_match.group(2))
            low, high = min(s, e), max(s, e)
            if max_eps:
                high = min(high, max_eps)
            nums.update(range(low, high + 1))
            continue

        prefix_match = re.match(r"^-(?:\.\.)?(\d+)$", token)
        if prefix_match:
            high = int(prefix_match.group(1))
            if max_eps:
                high = min(high, max_eps)
            nums.update(range(1, max(1, high) + 1))
            continue

        if max_eps and re.match(r"^(\d+)(?:-|\.\.)$", token):
            low = int(token.rstrip("-.").rstrip("."))
            if low <= max_eps:
                nums.update(range(low, max_eps + 1))
            continue

        if token.isdigit():
            val = int(token)
            if not max_eps or val <= max_eps:
                nums.add(val)

    return sorted(nums)


def sanitize_filename(filename: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', filename).strip()
    return cleaned or "download.mp4"


def parse_speed_limit(input_str: Optional[str]) -> Optional[int]:
    """Parse speed limit input like '2M', '500K', '1.5MB' into bytes/sec."""
    if not input_str:
        return None
    val = input_str.strip().upper()
    if val in ("0", "NONE", "UNLIMITED", "NO", "OFF", ""):
        return None
    m = re.match(r"^([\d.]+)\s*(K|KB|M|MB|G|GB)?$", val)
    if not m:
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "K").upper()
    if "G" in unit:
        return int(num * 1024 * 1024 * 1024)
    if "M" in unit:
        return int(num * 1024 * 1024)
    if "K" in unit:
        return int(num * 1024)
    return int(num)


def format_speed(bytes_per_sec: Optional[int]) -> str:
    if not bytes_per_sec or bytes_per_sec <= 0:
        return "Unlimited"
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
    return f"{bytes_per_sec / 1024:.0f} KB/s"


def prompt_download_options(
    console: Console,
    default_dir: str = "downloads",
    default_quality: str = "1080p",
    batch_mode: bool = False
) -> Optional[DownloadConfig]:
    """Interactive CLI prompt to ask for download preferences."""
    console.print()
    console.print("[bold cyan]─── Download Options ───[/bold cyan]")

    try:
        # 1. Save Directory
        path_prompt = console.input(f"  [cyan]Save Directory[/cyan] [dim]({default_dir})[/dim]: ").strip()
        save_path = Path(path_prompt or default_dir).expanduser().resolve()
        save_path.mkdir(parents=True, exist_ok=True)

        # 2. Quality
        q_prompt = console.input(f"  [cyan]Preferred Quality[/cyan] [dim]({default_quality} / 720p / 480p / best)[/dim]: ").strip()
        quality = q_prompt or default_quality

        # 3. Parallel workers (if multiple episodes)
        parallel = 2
        subtitle_lang = None
        if batch_mode:
            p_prompt = console.input("  [cyan]Parallel Downloads[/cyan] [dim](1-5, default 2)[/dim]: ").strip()
            if p_prompt.isdigit():
                parallel = max(1, min(5, int(p_prompt)))
            sub_prompt = console.input(
                "  [cyan]Subtitle Language[/cyan] [dim](blank=default, e.g. en, ar, off)[/dim]: "
            ).strip()
            subtitle_lang = sub_prompt or None

        # 4. Speed Limit
        s_prompt = console.input("  [cyan]Speed Limit[/cyan] [dim](0=Unlimited, or e.g. 2M, 500K)[/dim]: ").strip()
        speed_limit = parse_speed_limit(s_prompt)

        console.print(
            f"  [dim]Settings: Dir={save_path.name} | Quality={quality} | Parallel={parallel} | Speed={format_speed(speed_limit)}[/dim]\n"
        )
        return DownloadConfig(
            save_dir=save_path,
            max_parallel=parallel,
            speed_limit_bytes=speed_limit,
            quality=quality,
            subtitle_lang=subtitle_lang,
        )
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Download cancelled.[/yellow]")
        return None


class StreamDownloader:
    """Handles chunked resumable downloads with optional speed throttling."""

    def __init__(self, speed_limit_bytes: Optional[int] = None):
        self.speed_limit = speed_limit_bytes
        self.stop_requested = threading.Event()

    def download(
        self,
        url: str,
        dest_path: Path,
        progress: Progress,
        task_id: Any,
        headers: Optional[Dict[str, str]] = None
    ) -> bool:
        if self.stop_requested.is_set():
            return False

        part_path = dest_path.with_suffix(dest_path.suffix + ".part")

        # Skip if file already complete and valid
        if dest_path.exists() and dest_path.stat().st_size > 0 and not part_path.exists():
            file_size = dest_path.stat().st_size
            if progress and task_id is not None:
                progress.update(task_id, total=file_size, completed=file_size)
            return True

        auto_referer = get_stream_referer(url)
        req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", **(headers or {})}
        if auto_referer and "Referer" not in req_headers:
            req_headers["Referer"] = auto_referer

        resume_byte_pos = 0
        if part_path.exists():
            resume_byte_pos = part_path.stat().st_size
            if resume_byte_pos > 0:
                req_headers["Range"] = f"bytes={resume_byte_pos}-"

        try:
            with requests.get(url, headers=req_headers, stream=True, timeout=15) as resp:
                if self.stop_requested.is_set():
                    return False

                if resp.status_code == 416:
                    # The remote object changed or shrank. Never treat a partial
                    # file as complete just because the range was unsatisfiable.
                    req_headers.pop("Range", None)
                    with requests.get(url, headers=req_headers, stream=True, timeout=15) as r_full:
                        if self.stop_requested.is_set():
                            return False
                        r_full.raise_for_status()
                        total = r_full.headers.get("Content-Length")
                        return self._stream_to_disk(
                            r_full, part_path, dest_path, progress, task_id, 0,
                            int(total) if total else None,
                        )

                if resp.status_code not in (200, 206):
                    # Direct retry without range
                    req_headers.pop("Range", None)
                    with requests.get(url, headers=req_headers, stream=True, timeout=15) as r_full:
                        if self.stop_requested.is_set():
                            return False
                        r_full.raise_for_status()
                        total = r_full.headers.get("Content-Length")
                        return self._stream_to_disk(
                            r_full, part_path, dest_path, progress, task_id, 0,
                            int(total) if total else None,
                        )

                total_length = resp.headers.get("Content-Length")
                if resp.status_code == 206:
                    initial_offset = resume_byte_pos
                    total_size = int(total_length) + resume_byte_pos if total_length else None
                else:
                    # Server returned 200 (full content from start)
                    initial_offset = 0
                    resume_byte_pos = 0
                    total_size = int(total_length) if total_length else None

                progress.update(task_id, total=total_size, completed=resume_byte_pos)
                return self._stream_to_disk(
                    resp, part_path, dest_path, progress, task_id, initial_offset, total_size
                )

        except Exception as e:
            logger.error("DOWNLOAD", f"Download failed for {dest_path.name}: {e}")
            return False

    def _stream_to_disk(
        self,
        resp: requests.Response,
        part_path: Path,
        final_path: Path,
        progress: Progress,
        task_id: Any,
        initial_offset: int,
        expected_size: Optional[int] = None,
    ) -> bool:
        chunk_size = 64 * 1024  # 64KB
        mode = "ab" if initial_offset > 0 else "wb"

        with open(part_path, mode) as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if self.stop_requested.is_set():
                    return False
                if not chunk:
                    continue

                t0 = time.time()
                f.write(chunk)
                progress.update(task_id, advance=len(chunk))

                # Rate limiting
                if self.speed_limit and self.speed_limit > 0:
                    expected_time = len(chunk) / self.speed_limit
                    elapsed = time.time() - t0
                    if expected_time > elapsed:
                        time.sleep(expected_time - elapsed)

        size = part_path.stat().st_size if part_path.exists() else 0
        if size <= 0:
            logger.error("DOWNLOAD", f"Empty download discarded: {final_path.name}")
            part_path.unlink(missing_ok=True)
            return False
        if expected_size is not None and size != expected_size:
            logger.error(
                "DOWNLOAD",
                f"Incomplete download for {final_path.name}: {size} of {expected_size} bytes",
            )
            return False

        part_path.replace(final_path)
        return True


def resolve_episode_download_url(
    api: Any,
    anime: Any,
    ep: Episode,
    preferred_quality: str = "1080p"
) -> Optional[str]:
    """Extract direct download stream URL for an episode with multi-provider fallback."""
    target = resolve_episode_download_target(api, anime, ep, preferred_quality)
    return target[0] if target else None


def is_hls_url(url: str) -> bool:
    return url.split("?", 1)[0].lower().endswith(".m3u8")


def episode_filename(anime: Any, ep: Episode, quality: str, ext: str = "mp4") -> str:
    safe_ext = str(ext or "mp4").lstrip(".") or "mp4"
    return sanitize_filename(f"{anime.title_en} - Ep {ep.display_num} [{quality}].{safe_ext}")


def is_already_downloaded(dest_path: Path) -> bool:
    return find_existing_download(dest_path) is not None


def find_episode_download(save_dir: Path, anime: Any, ep: Episode) -> Optional[Path]:
    """Find a completed file for this episode regardless of quality or container."""
    try:
        base = Path(save_dir)
        if not base.is_dir():
            return None
        prefix = sanitize_filename(f"{anime.title_en} - Ep {ep.display_num} [")
        for pattern in ("*.mp4", "*.ts", "*.mkv"):
            for path in base.glob(pattern):
                part = Path(str(path) + ".part")
                if path.name.startswith(prefix) and path.stat().st_size > 0 and not part.exists():
                    return path
    except OSError:
        return None
    return None


VIDEO_EXTENSIONS = (".mp4", ".ts", ".mkv")
SUBTITLE_EXTENSIONS = (".vtt", ".srt")


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def list_completed_downloads(save_dir: Path) -> List[Dict[str, Any]]:
    """List finished video files (plus subtitle sidecar count) in a directory."""
    base = Path(save_dir)
    if not base.is_dir():
        return []
    items: List[Dict[str, Any]] = []
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    for path in entries:
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if Path(str(path) + ".part").exists():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 0:
            continue
        subtitles = [
            sibling
            for sibling in base.iterdir()
            if sibling.is_file()
            and sibling.name.startswith(path.stem + ".")
            and sibling.suffix.lower() in SUBTITLE_EXTENSIONS
            and sibling != path
        ]
        items.append(
            {
                "path": path,
                "name": path.name,
                "size": size,
                "size_label": _format_size(size),
                "subtitles": sorted(subtitles),
            }
        )
    items.sort(key=lambda item: item["name"].lower())
    return items


def remove_completed_download(path: Path) -> bool:
    """Delete a downloaded video and its subtitle sidecars. Never touches .part files."""
    path = Path(path)
    base = path.parent
    removed = False
    try:
        sidecars = [
            sibling
            for sibling in base.iterdir()
            if sibling.is_file()
            and sibling.name.startswith(path.stem + ".")
            and sibling.suffix.lower() in SUBTITLE_EXTENSIONS
            and sibling != path
        ]
        for sidecar in sidecars:
            sidecar.unlink(missing_ok=True)
        path.unlink()
        removed = True
    except OSError as error:
        logger.error("DOWNLOAD", f"Could not remove {path.name}: {error}")
    return removed


def _hls_error_message(error: Exception) -> str:
    if isinstance(error, HlsDrmError):
        return f"DRM-protected stream; downloading is unsupported ({error})"
    if isinstance(error, HlsExpiredError):
        return f"stream link expired; re-resolve the episode ({error})"
    if isinstance(error, HlsDiskSpaceError):
        return str(error)
    if isinstance(error, HlsCancelledError):
        return "cancelled"
    if isinstance(error, HlsError):
        return str(error)
    return str(error) or error.__class__.__name__


def _extract_quality_label(name: str, fallback: str = "auto") -> str:
    match = re.search(r"\b(\d{3,4}p)\b", str(name or ""))
    return match.group(1) if match else (str(name or "").strip() or fallback)


def _choose_subtitle(subtitles: List[Dict[str, Any]], language: Optional[str]) -> Optional[Dict[str, Any]]:
    if not subtitles:
        return None
    if language:
        wanted = str(language).strip().lower()
        if wanted in ("off", "none", "no", "0", "false"):
            return None
        for track in subtitles:
            lang = str(track.get("lang") or "").lower()
            label = str(track.get("label") or "").lower()
            if wanted == lang or wanted in label or lang.startswith(wanted) or wanted.startswith(lang):
                return track
    for track in subtitles:
        if track.get("default"):
            return track
    return subtitles[0]


def resolve_download_plan(
    api: Any,
    anime: Any,
    ep: Episode,
    preferred_quality: str = "1080p",
    subtitle_lang: Optional[str] = None,
) -> Optional[DownloadPlan]:
    """Resolve stream URL, headers, container and subtitle sidecar for an episode."""
    try:
        ep_num = getattr(ep, "number", "1")
        anime_id = getattr(anime, "id", "")
        anime_type = getattr(anime, "type", "SERIES")
        s_data = api.get_streaming_servers(anime_id, ep_num, anime_type)
        if not s_data:
            return None

        clean_pref = str(preferred_quality or "1080p").lower().replace("p", "")
        qualities = s_data.get("Qualities", []) if isinstance(s_data, dict) else []
        if qualities:
            target = None
            for q in qualities:
                q_name = getattr(q, "name", "").lower()
                if clean_pref and clean_pref in q_name:
                    target = q
                    break
            if not target:
                target = qualities[0]

            direct_url = getattr(target, "direct_url", None)
            if not direct_url and isinstance(s_data, dict):
                current_ep = s_data.get("CurrentEpisode", {})
                direct_url = current_ep.get(getattr(target, "server_key", ""))
            if direct_url and direct_url.startswith("http"):
                subtitles = [
                    dict(track)
                    for track in (getattr(target, "subtitles", None) or [])
                    if isinstance(track, dict) and track.get("src")
                ]
                chosen = _choose_subtitle(subtitles, subtitle_lang)
                return DownloadPlan(
                    url=direct_url,
                    headers=dict(getattr(target, "headers", None) or {}),
                    quality=_extract_quality_label(getattr(target, "name", ""), preferred_quality),
                    ext=str(getattr(target, "ext", "mp4") or "mp4").lstrip("."),
                    subtitle_url=str((chosen or {}).get("src") or ""),
                    subtitle_lang=str((chosen or {}).get("lang") or ""),
                    subtitles=subtitles,
                    master_url=str(getattr(target, "master_url", "") or ""),
                    audio_lang=str(getattr(target, "audio_lang", "") or ""),
                )

        # 2. Fallback to DirectStreams dict
        if isinstance(s_data, dict) and "DirectStreams" in s_data:
            direct_streams = s_data["DirectStreams"]
            for key in (preferred_quality, f"{clean_pref}p", "1080p", "720p", "480p"):
                if key in direct_streams and str(direct_streams[key]).startswith("http"):
                    return DownloadPlan(
                        url=direct_streams[key],
                        headers={},
                        quality=_extract_quality_label(key, preferred_quality),
                    )
            for value in direct_streams.values():
                if str(value).startswith("http"):
                    return DownloadPlan(url=value, headers={}, quality=preferred_quality)

        # 3. Fallback to CurrentEpisode dict keys (legacy keys)
        if isinstance(s_data, dict) and "CurrentEpisode" in s_data:
            cur_ep = s_data["CurrentEpisode"]
            key_map = {"1080": "FRFhdQ", "720": "FRLink", "480": "FRLowQ"}
            target_key = key_map.get(clean_pref, "FRFhdQ")
            if target_key in cur_ep and cur_ep[target_key]:
                return DownloadPlan(
                    url=cur_ep[target_key],
                    headers={},
                    quality=_extract_quality_label(target_key, preferred_quality),
                )
            for fallback_key in ["FRFhdQ", "FRLink", "FRLowQ"]:
                if fallback_key in cur_ep and cur_ep[fallback_key]:
                    return DownloadPlan(
                        url=cur_ep[fallback_key],
                        headers={},
                        quality=_extract_quality_label(fallback_key, preferred_quality),
                    )

        return None
    except Exception as e:
        title = getattr(anime, "title_en", "") or getattr(anime, "title_ar", "") or "Anime"
        logger.error("DOWNLOAD", f"Error resolving link for {title} ep {getattr(ep, 'number', '?')}: {e}")
        return None


def resolve_episode_download_target(
    api: Any,
    anime: Any,
    ep: Episode,
    preferred_quality: str = "1080p"
) -> Optional[Tuple[str, Dict[str, str]]]:
    """Extract (direct stream URL, required request headers) for an episode with multi-provider fallback."""
    plan = resolve_download_plan(api, anime, ep, preferred_quality)
    return (plan.url, dict(plan.headers)) if plan else None


def download_single_episode(
    api: Any,
    anime: Any,
    ep: Episode,
    config: DownloadConfig,
    console: Console,
    plan: Optional[DownloadPlan] = None,
    preferred_quality: Optional[str] = None,
    overwrite: bool = False,
    show_progress: bool = True,
) -> Dict[str, Any]:
    """Download one episode, native-HLS or direct, returning a structured result.

    ``plan`` lets the caller reuse a URL the user just picked in the quality menu,
    so the signed link is not resolved twice (and does not expire in between).
    """
    result: Dict[str, Any] = {
        "success": False,
        "path": None,
        "subtitle": None,
        "quality": preferred_quality or config.quality,
        "skipped": False,
        "error": "",
    }

    if not overwrite:
        requested_quality = preferred_quality or config.quality
        existing = find_existing_download(
            config.save_dir / episode_filename(anime, ep, requested_quality)
        )
        if existing:
            result.update(success=True, path=existing, skipped=True)
            if show_progress:
                console.print(f"[dim]Ep {ep.display_num} already downloaded: {existing}[/dim]")
            return result

    target_quality = preferred_quality or config.quality
    if plan is None:
        if show_progress:
            console.print(f"[dim]Resolving Ep {ep.display_num}...[/dim]")
        plan = resolve_download_plan(api, anime, ep, target_quality, config.subtitle_lang)
    if plan is None:
        result["error"] = "could not resolve a download link"
        return result

    result["quality"] = plan.quality or target_quality
    try:
        config.save_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        result["error"] = f"cannot create download directory: {error}"
        return result

    dest_path = config.save_dir / episode_filename(anime, ep, result["quality"], plan.ext)
    if not overwrite:
        existing = find_existing_download(dest_path)
        if existing:
            result.update(success=True, path=existing, skipped=True)
            if show_progress:
                console.print(f"[dim]Ep {ep.display_num} already downloaded: {existing}[/dim]")
            return result
    downloader = StreamDownloader(speed_limit_bytes=config.speed_limit_bytes)

    progress_columns = [
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=30),
        "[progress.percentage]{task.percentage:>3.0f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    ]

    def _run_hls(task_id=None, progress=None) -> Path:
        hls = HlsDownloader(
            headers=plan.headers,
            speed_limit_bytes=config.speed_limit_bytes,
            stop_event=downloader.stop_requested,
        )

        def _on_progress(done, total, _done_bytes, _total_bytes, label):
            if progress is not None and task_id is not None:
                progress.update(
                    task_id,
                    total=total,
                    completed=done,
                    description=f"Ep {ep.display_num} {label}",
                )

        return hls.download(
            plan.master_url or plan.url,
            dest_path,
            preferred_quality=plan.quality or target_quality,
            audio_lang=plan.audio_lang,
            progress=_on_progress if show_progress else None,
        ).path

    def _run_direct(task_id=None, progress=None) -> Optional[Path]:
        ok = downloader.download(plan.url, dest_path, progress, task_id, headers=plan.headers)
        return dest_path if ok else None

    try:
        if show_progress:
            with Progress(*progress_columns, console=console, transient=False) as progress:
                task_id = progress.add_task(
                    f"Ep {ep.display_num} {result['quality']}",
                    total=None,
                )
                final_path = _run_hls(task_id, progress) if plan.is_hls else _run_direct(task_id, progress)
        else:
            final_path = _run_hls() if plan.is_hls else _run_direct()
    except KeyboardInterrupt:
        downloader.stop_requested.set()
        result["error"] = "cancelled (partial data kept for resume)"
        return result
    except HlsCancelledError:
        result["error"] = "cancelled (partial data kept for resume)"
        return result
    except (HlsError, HlsDiskSpaceError) as error:
        result["error"] = _hls_error_message(error)
        return result
    except Exception as error:
        result["error"] = str(error) or error.__class__.__name__
        return result

    if not final_path or not Path(final_path).exists() or Path(final_path).stat().st_size <= 0:
        result["error"] = (
            "cancelled (partial data kept for resume)"
            if downloader.stop_requested.is_set()
            else "download failed or produced an empty file"
        )
        return result

    if plan.subtitle_url:
        result["subtitle"] = download_subtitle(
            plan.subtitle_url,
            Path(final_path),
            plan.subtitle_lang or config.subtitle_lang or "",
            headers=plan.headers,
        )

    result.update(success=True, path=Path(final_path))
    return result


def execute_batch_download(
    api: Any,
    anime: Any,
    episodes: List[Episode],
    config: DownloadConfig,
    console: Console,
    history_manager: Optional[Any] = None
) -> Dict[str, int]:
    """Downloads episodes concurrently with a Rich progress dashboard.

    Episodes already on disk are skipped before any link is resolved. Links are
    resolved just before each download so signed URLs don't expire in the queue.
    A failed episode never stops the batch; failures are listed at the end.
    """
    pending: List[Tuple[Episode, Path]] = []
    skipped = 0
    for ep in episodes:
        dest_path = config.save_dir / episode_filename(anime, ep, config.quality)
        if find_existing_download(dest_path):
            skipped += 1
        else:
            pending.append((ep, dest_path))

    if skipped:
        console.print(f"\n[dim]Skipping {skipped} episode(s) already downloaded.[/dim]")
    if not pending:
        console.print("[bold green]✓ All selected episodes are already downloaded.[/bold green]")
        console.print(f"[dim]Saved to: {config.save_dir}[/dim]\n")
        return {"success": 0, "failed": 0, "skipped": skipped}

    total = len(pending)
    console.print(f"\n[cyan]Downloading {total} episode(s) of {anime.title_en}...[/cyan]")

    downloader = StreamDownloader(speed_limit_bytes=config.speed_limit_bytes)
    # Providers are scraped one request at a time, as before; only file transfers run in parallel.
    resolve_lock = threading.Lock()
    failures: List[Tuple[Episode, str]] = []
    succeeded = 0
    done = 0
    cancelled = False

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=30),
        "[progress.percentage]{task.percentage:>3.0f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False
    ) as progress:
        overall_task = progress.add_task(f"[bold green]Downloading 0/{total}", total=total)

        def _worker(ep: Episode, dest_path: Path) -> Tuple[Episode, Optional[str]]:
            if downloader.stop_requested.is_set():
                return ep, "cancelled"
            task_id = progress.add_task(f"Ep {ep.display_num} (resolving)", total=None)
            try:
                with resolve_lock:
                    plan = resolve_download_plan(
                        api, anime, ep, config.quality, config.subtitle_lang
                    )
                if not plan:
                    return ep, "could not resolve a download link"
                progress.update(task_id, description=f"Ep {ep.display_num} {plan.quality}")

                final_path = dest_path
                if plan.is_hls:
                    hls = HlsDownloader(
                        headers=plan.headers,
                        speed_limit_bytes=config.speed_limit_bytes,
                        stop_event=downloader.stop_requested,
                    )

                    def _on_progress(done, total, _bytes_done, _bytes_total, label):
                        progress.update(
                            task_id,
                            total=total,
                            completed=done,
                            description=f"Ep {ep.display_num} {label}",
                        )

                    result = hls.download(
                        plan.master_url or plan.url,
                        dest_path,
                        preferred_quality=plan.quality,
                        audio_lang=plan.audio_lang,
                        progress=_on_progress,
                    )
                    final_path = result.path
                else:
                    ok = downloader.download(
                        plan.url, dest_path, progress, task_id, headers=plan.headers
                    )
                    if not ok:
                        return ep, (
                            "cancelled"
                            if downloader.stop_requested.is_set()
                            else "download failed (see log)"
                        )

                if plan.subtitle_url:
                    download_subtitle(
                        plan.subtitle_url,
                        final_path,
                        plan.subtitle_lang or config.subtitle_lang or "",
                        headers=plan.headers,
                    )
                return ep, None
            except HlsCancelledError:
                return ep, "cancelled"
            except (HlsError, HlsDiskSpaceError) as e:
                return ep, _hls_error_message(e)
            except Exception as e:
                return ep, str(e) or e.__class__.__name__
            finally:
                progress.remove_task(task_id)

        executor = ThreadPoolExecutor(max_workers=config.max_parallel)
        futures = {executor.submit(_worker, ep, dest_path) for ep, dest_path in pending}
        try:
            while futures:
                # Poll with a timeout so Ctrl+C is handled promptly (blocking waits ignore it on Windows).
                finished, futures = wait(futures, timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in finished:
                    ep, error = fut.result()
                    done += 1
                    if error is None:
                        succeeded += 1
                        if history_manager:
                            try:
                                history_manager.mark_watched(
                                    anime.id,
                                    ep.display_num,
                                    anime.title_en,
                                    poster_url=getattr(anime, 'thumbnail', ''),
                                    provider=config.provider,
                                    language_mode=config.language_mode,
                                )
                            except Exception:
                                pass
                    else:
                        failures.append((ep, error))
                        logger.error("DOWNLOAD", f"Batch: {anime.title_en} ep {ep.display_num} failed: {error}")
                    progress.update(overall_task, advance=1, description=f"[bold green]Downloading {done}/{total}")
            executor.shutdown(wait=True)
        except KeyboardInterrupt:
            cancelled = True
            downloader.stop_requested.set()
            executor.shutdown(wait=False, cancel_futures=True)

    if cancelled:
        console.print("\n[yellow]Batch download cancelled by user. Partial files are kept and resume next time.[/yellow]")

    failures = [(ep, err) for ep, err in failures if err != "cancelled"]
    summary = f"\n[bold green]Download finished:[/bold green] {succeeded} succeeded, {len(failures)} failed"
    if skipped:
        summary += f", {skipped} skipped (already downloaded)"
    console.print(summary + ".")
    if failures:
        console.print("[bold red]Failed episodes:[/bold red]")
        for ep, err in sorted(failures, key=lambda f: _episode_sort_key(f[0])):
            console.print(f"  [red]✖ Ep {ep.display_num}[/red] [dim]— {err}[/dim]")
    console.print(f"[dim]Saved to: {config.save_dir}[/dim]\n")
    return {"success": succeeded, "failed": len(failures), "skipped": skipped}


def _episode_sort_key(ep: Episode) -> float:
    try:
        return float(ep.display_num)
    except (TypeError, ValueError):
        return float("inf")
