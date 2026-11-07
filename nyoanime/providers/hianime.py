"""
HiAnime English provider for NyoAnime.

The search, episode, ZokoAnime embed and HLS resolution flow is a Python port
of pystardust/ani-cli 5.1.6 (GPL-3.0):
    https://github.com/pystardust/ani-cli
NyoAnime is also GPL-3.0.  See NOTICE for attribution.

Only the provider logic was adapted; no shell pipeline or remote code from
ani-cli is executed.  HiAnime is protected by Cloudflare, so requests are
attempted with a browser-like session and transparently retried through
curl-cffi's TLS impersonation when necessary.
"""

import base64
import html as html_lib
import json
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, quote, quote_plus, urljoin, urlparse

import requests

from .base import BaseAnimeProvider
from ..logger import logger
from ..models import AnimeResult, Episode, QualityOption

_XOR_KEY = b"otaku-embed-v1"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def _attr(tag: str, name: str) -> str:
    match = re.search(rf'\b{re.escape(name)}="([^"]*)"', tag or "")
    return html_lib.unescape(match.group(1)).strip() if match else ""


def _text(value: str) -> str:
    return _SPACE_RE.sub(" ", _TAG_RE.sub(" ", value or "")).strip()


def _int(value: str, default: int = 0) -> int:
    match = re.search(r"\d+", value or "")
    return int(match.group(0)) if match else default


def _absolute(url: str, base: str) -> str:
    if not url:
        return ""
    return urljoin(base, html_lib.unescape(url))


def _slug(href: str) -> str:
    path = urlparse(href or "").path.strip("/")
    parts = [part for part in path.split("/") if part]
    if parts and parts[0] == "watch":
        parts = parts[1:]
    return parts[-1] if parts else ""


class HiAnimeProvider(BaseAnimeProvider):
    """HiAnime English subbed provider (native port of ani-cli's HiAnime flow)."""

    id = "hianime"
    name = "HiAnime (English Sub)"
    description = "English subbed anime with HLS quality and subtitle tracks"

    MODE = "sub"
    language = "en"
    audio_mode = "sub"
    language_mode = "en_sub"

    BASE_URL = "https://hianime.at"
    EMBED_FALLBACK_NAME = "ZokoAnime"

    supported_features: Set[str] = {
        "search",
        "latest",
        "movies",
        "trending",
        "popular",
        "top_rated",
        "genres",
        "studios",
    }

    def __init__(self):
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=25, pool_maxsize=25, max_retries=3
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "User-Agent": _DEFAULT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            }
        )
        self._curl_session = None
        self._detail_cache: Dict[str, AnimeResult] = {}
        self._episode_cache: Dict[str, List[Episode]] = {}
        self._availability: Dict[str, Dict[str, int]] = {}

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, headers: Dict[str, str]):
        try:
            from curl_cffi import requests as curl_requests
        except Exception:
            return None
        try:
            if self._curl_session is None:
                self._curl_session = curl_requests.Session(impersonate="chrome")
                self._curl_session.headers.update(self.session.headers)
            return self._curl_session.get(url, headers=headers, timeout=5)
        except Exception as exc:
            logger.debug("HIANIME", f"curl-cffi fallback failed: {exc}")
            return None

    def _get(self, url: str, referer: str = "", params: Optional[dict] = None):
        headers = {"Referer": referer} if referer else {}
        try:
            response = self.session.get(url, headers=headers, params=params, timeout=(3, 5))
        except Exception as exc:
            logger.debug("HIANIME", f"Request failed: {url} ({exc})")
            response = None

        blocked = response is None or response.status_code in (403, 503)
        if not blocked:
            sample = response.text[:4000].lower()
            blocked = "just a moment" in sample or "cf-chl" in sample or "attention required" in sample

        if blocked:
            fallback = self._curl_get(url, headers)
            if fallback is not None:
                return fallback
            if response is None:
                return None
            if response.status_code in (403, 503):
                return None
            # Cloudflare interstitial with a 200 is still unusable.
            return None
        return response

    def _get_text(self, url: str, referer: str = "", params: Optional[dict] = None) -> str:
        response = self._get(url, referer=referer, params=params)
        return (response.text or "") if response is not None else ""

    def _get_json(self, url: str, referer: str = "", params: Optional[dict] = None) -> Dict[str, Any]:
        response = self._get(url, referer=referer, params=params)
        if response is None:
            return {}
        try:
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_film_items(self, page: str) -> List[AnimeResult]:
        if not page:
            return []
        # The top-10 sidebar repeats result markup; cut it before parsing.
        page = page.split('id="main-sidebar"')[0]
        results: List[AnimeResult] = []
        seen = set()
        for block in re.split(r'<div class="flw-item\b', page)[1:]:
            heading = re.search(r'<h3 class="film-name">(.*?)</h3>', block, re.DOTALL)
            if not heading:
                continue
            anchor = re.search(r"<a\b[^>]*>", heading.group(1), re.DOTALL)
            if not anchor:
                continue
            tag = anchor.group(0)
            href = _attr(tag, "href")
            anime_id = _slug(href)
            if not anime_id or anime_id in seen:
                continue
            seen.add(anime_id)

            title = _attr(tag, "title") or _text(heading.group(1))
            jname = _attr(tag, "data-jname") or _attr(heading.group(1), "data-jname")

            poster = ""
            img = re.search(
                r'<img\b[^>]*class="[^"]*film-poster-img[^"]*"[^>]*>', block, re.DOTALL
            )
            if img:
                poster = _absolute(_attr(img.group(0), "src"), self.BASE_URL)

            anime_type = "SERIES"
            type_match = re.search(r'<span class="fdi-item">([^<]+)</span>', block)
            if type_match:
                anime_type = type_match.group(1).strip()

            duration = ""
            duration_match = re.search(
                r'<span class="fdi-item fdi-duration">([^<]+)</span>', block
            )
            if duration_match:
                duration = duration_match.group(1).strip()

            sub_count = _int(self._tick(block, "sub"))
            dub_count = _int(self._tick(block, "dub"))
            ep_count = _int(self._tick(block, "eps"))

            available = []
            if sub_count:
                available.append("sub")
            if dub_count:
                available.append("dub")
            language = self.MODE if self.MODE in available or not available else available[0]

            results.append(
                AnimeResult(
                    id=anime_id,
                    title_en=title,
                    title_jp=jname,
                    title_romaji=jname,
                    type=anime_type or "SERIES",
                    episodes=str(ep_count) if ep_count else "N/A",
                    duration=duration or "N/A",
                    thumbnail=poster,
                    language=language,
                )
            )
        return results

    @staticmethod
    def _tick(block: str, name: str) -> str:
        match = re.search(
            rf'<div class="tick-item tick-{name}"[^>]*>(.*?)</div>', block, re.DOTALL
        )
        return _text(match.group(1)) if match else ""

    def _section(self, page: str, heading: str) -> str:
        match = re.search(rf'<h2 class="cat-heading">{re.escape(heading)}</h2>', page)
        if not match:
            return page
        start = match.end()
        following = re.search(r'<h2 class="cat-heading">', page[start:])
        end = start + following.start() if following else len(page)
        return page[start:end]

    def _paged_films(self, path: str, from_index: int = 0, limit: int = 20) -> List[AnimeResult]:
        offset = max(0, int(from_index or 0))
        target = offset + max(1, int(limit or 20))
        collected: List[AnimeResult] = []
        page = offset // 30 + 1
        offset_in_page = offset % 30
        # HiAnime returns 30 items per page; two pages is the practical UI cap.
        for _ in range(6):
            params = {"page": page} if page > 1 else None
            page_html = self._get_text(f"{self.BASE_URL}{path}", params=params)
            items = self._parse_film_items(page_html)
            if not items:
                break
            collected.extend(items)
            if len(collected) >= target:
                break
            page += 1
        return collected[offset_in_page:offset_in_page + max(1, int(limit or 20))]

    # ------------------------------------------------------------------
    # Search / details
    # ------------------------------------------------------------------

    def search_anime(self, query: str) -> List[AnimeResult]:
        query = (query or "").strip()
        if not query:
            return []
        page = self._get_text(f"{self.BASE_URL}/search?keyword={quote_plus(query)}")
        return self._parse_film_items(page)

    def get_anime_details(self, anime_id: str) -> AnimeResult:
        slug = _slug(anime_id) or str(anime_id or "").strip().strip("/")
        if not slug:
            return AnimeResult(id="", title_en="", language=self.MODE)
        if slug in self._detail_cache:
            return self._detail_cache[slug]

        page = self._get_text(f"{self.BASE_URL}/{slug}")
        if not page:
            fallback = AnimeResult(
                id=slug,
                title_en=slug.replace("-", " ").title(),
                language=self.MODE,
            )
            return fallback

        title_match = re.search(
            r'<h2 class="film-name[^"]*"[^>]*>(.*?)</h2>', page, re.DOTALL
        )
        title_block = title_match.group(1) if title_match else ""
        title = _text(title_block) or slug.replace("-", " ").title()
        jname = _attr(title_match.group(0), "data-jname") if title_match else ""
        if not jname:
            anchor = re.search(r"<a\b[^>]*>", title_block, re.DOTALL)
            jname = _attr(anchor.group(0), "data-jname") if anchor else ""

        poster = ""
        poster_block = re.search(r'<div class="anisc-poster">(.*?)</div>\s*</div>', page, re.DOTALL)
        if poster_block:
            img = re.search(r'<img\b[^>]*>', poster_block.group(1), re.DOTALL)
            if img:
                poster = _absolute(_attr(img.group(0), "src"), self.BASE_URL)

        synopsis = ""
        overview = re.search(
            r'<span class="item-head">Overview:</span>\s*<div class="text">(.*?)</div>',
            page,
            re.DOTALL,
        )
        if overview:
            synopsis = _text(overview.group(1))
        else:
            description = re.search(
                r'<div class="film-description[^"]*">\s*<div class="text">(.*?)</div>',
                page,
                re.DOTALL,
            )
            if description:
                synopsis = _text(description.group(1))

        info = {
            "japanese": self._info_name(page, "Japanese"),
            "aired": self._info_name(page, "Aired"),
            "duration": self._info_name(page, "Duration"),
            "status": self._info_name(page, "Status"),
            "score": self._info_name(page, "MAL Score"),
            "type": self._info_name(page, "Type"),
        }

        genres = self._info_links(page, "Genres")
        studios = self._info_links(page, "Studios")
        producers = self._info_links(page, "Producers")

        sub_count = _int(self._tick(page, "sub"))
        dub_count = _int(self._tick(page, "dub"))
        ep_count = _int(self._tick(page, "eps"))
        self._availability[slug] = {"sub": sub_count, "dub": dub_count}

        rating = ""
        pg = re.search(r'<div class="tick-item tick-pg">([^<]*)</div>', page)
        if pg:
            rating = _text(pg.group(1))

        stats_items = re.findall(r'<span class="item">([^<]+)</span>', page)
        anime_type = stats_items[0].strip() if stats_items else "SERIES"
        duration = info["duration"] or (stats_items[1].strip() if len(stats_items) > 1 else "")

        result = AnimeResult(
            id=slug,
            title_en=title,
            title_jp=jname or info["japanese"],
            title_romaji=jname or info["japanese"],
            type=anime_type,
            episodes=str(ep_count) if ep_count else "N/A",
            status=info["status"] or "N/A",
            genres=", ".join(genres) or "N/A",
            score=info["score"] or "N/A",
            rating=rating or "N/A",
            premiered=info["aired"] or "N/A",
            creators=", ".join(producers) or "N/A",
            studio=", ".join(studios) or "",
            duration=duration or "N/A",
            thumbnail=poster,
            synopsis=synopsis,
            language=self.MODE,
        )
        self._detail_cache[slug] = result
        return result

    @staticmethod
    def _info_name(page: str, label: str) -> str:
        match = re.search(
            rf'<span class="item-head">{re.escape(label)}:</span>\s*<span class="name">(.*?)</span>',
            page,
            re.DOTALL,
        )
        return _text(match.group(1)) if match else ""

    @staticmethod
    def _info_links(page: str, label: str) -> List[str]:
        match = re.search(
            rf'<span class="item-head">{re.escape(label)}:</span>(.*?)(?=<div class="item |</div>\s*</div>)',
            page,
            re.DOTALL,
        )
        if not match:
            return []
        values = []
        for link in re.finditer(r'<a\b[^>]*>', match.group(1)):
            value = _attr(link.group(0), "title") or _text(link.group(0))
            if value and value not in values:
                values.append(value)
        return values

    # ------------------------------------------------------------------
    # Episodes
    # ------------------------------------------------------------------

    def get_episodes(self, anime_id: str) -> List[Episode]:
        slug = _slug(anime_id) or str(anime_id or "").strip().strip("/")
        if slug in self._episode_cache:
            return list(self._episode_cache[slug])

        number_id = self._numeric_id(slug)
        if not number_id:
            return []

        payload = self._get_json(f"{self.BASE_URL}/api/theme/episode/list/{number_id}")
        page = payload.get("html", "")
        if not page:
            return []

        availability = self._availability.get(slug)
        if availability is None:
            self.get_anime_details(slug)
            availability = self._availability.get(slug, {})

        episodes: List[Episode] = []
        seen = set()
        for tag in re.findall(
            r'<a\b(?=[^>]*\bclass="[^"]*\bep-item\b)[^>]*>', page, re.DOTALL
        ):
            number = _attr(tag, "data-number")
            episode_id = _attr(tag, "data-id")
            href = _absolute(_attr(tag, "href"), self.BASE_URL)
            if not number or not episode_id or episode_id in seen:
                continue
            seen.add(episode_id)

            try:
                display_num = int(float(number))
            except (TypeError, ValueError):
                display_num = len(episodes) + 1

            modes = self._modes_for_episode(number, availability)
            language = self.MODE if self.MODE in modes or not modes else modes[0]
            episodes.append(
                Episode(
                    number=str(number),
                    display_num=display_num,
                    title=_attr(tag, "title") or f"Episode {number}",
                    url=href,
                    anime_id=slug,
                    language=language,
                    available_modes=modes,
                )
            )

        self._episode_cache[slug] = episodes
        return list(episodes)

    def refresh_anime_cache(self, anime_id: str) -> None:
        slug = _slug(anime_id) or str(anime_id or "").strip()
        self._detail_cache.pop(slug, None)
        self._episode_cache.pop(slug, None)
        self._availability.pop(slug, None)

    @staticmethod
    def _modes_for_episode(number: str, availability: Dict[str, int]) -> List[str]:
        modes: List[str] = []
        try:
            ep_value = int(float(number))
        except (TypeError, ValueError):
            return ["sub", "dub"]
        sub_count = int(availability.get("sub") or 0)
        dub_count = int(availability.get("dub") or 0)
        if sub_count and (ep_value <= sub_count or not dub_count):
            modes.append("sub")
        if dub_count and ep_value <= dub_count:
            modes.append("dub")
        if not modes and not sub_count and not dub_count:
            modes = ["sub", "dub"]
        return modes

    @staticmethod
    def _numeric_id(slug: str) -> str:
        match = re.search(r"(\d+)$", slug or "")
        return match.group(1) if match else ""

    @staticmethod
    def _episode_id_from_url(url: str) -> str:
        values = parse_qs(urlparse(str(url or "")).query).get("ep")
        return values[0] if values else ""

    def _find_episode_id(self, anime_id: str, episode_num: str) -> str:
        episodes = self.get_episodes(anime_id)
        target = str(episode_num or "").strip()
        for episode in episodes:
            if str(episode.number) == target:
                return self._episode_id_from_url(episode.url)
        try:
            target_value = float(target)
        except (TypeError, ValueError):
            return ""
        for episode in episodes:
            try:
                if float(episode.number) == target_value:
                    return self._episode_id_from_url(episode.url)
            except (TypeError, ValueError):
                continue
        return ""

    # ------------------------------------------------------------------
    # Streams
    # ------------------------------------------------------------------

    def get_streaming_servers(
        self, anime_id: str, episode_num: str, anime_type: str = "SERIES"
    ) -> Optional[Dict[str, Any]]:
        slug = _slug(anime_id) or str(anime_id or "").strip()
        episode_id = self._find_episode_id(slug, episode_num)
        if not episode_id:
            logger.debug("HIANIME", f"Episode {episode_num} not found for {slug}")
            return None

        servers_page = self._get_json(
            f"{self.BASE_URL}/api/theme/episode/servers?episodeId={episode_id}"
        ).get("html", "")
        if not servers_page:
            return None

        embed_hash = self._server_hash(servers_page, self.MODE)
        if not embed_hash:
            logger.info(
                "HIANIME",
                f"No {self.MODE} ZokoAnime server for {slug} episode {episode_num}",
            )
            return None

        embed_url = self._decode_hash(embed_hash)
        if not embed_url or not embed_url.startswith("http"):
            logger.warning("HIANIME", "Invalid ZokoAnime embed hash")
            return None

        embed_origin = self._origin(embed_url)
        embed_page = self._get_text(embed_url, referer=self.BASE_URL + "/")
        if not embed_page:
            return None

        blob = re.search(r'window\.__P="([^"]+)"', embed_page)
        if not blob:
            logger.warning("HIANIME", "Embed page is missing the stream blob")
            return None

        payload = self._deobfuscate(blob.group(1))
        if not payload:
            return None

        master = str(payload.get("src") or "")
        if not master.startswith("http"):
            logger.warning("HIANIME", "Embed payload has no valid HLS source")
            return None

        subtitles = self._normalize_subtitles(payload.get("subtitles"))
        subtitle_url = ""
        subtitle_lang = ""
        if self.MODE == "sub" and subtitles:
            chosen = next(
                (item for item in subtitles if item.get("default")), subtitles[0]
            )
            subtitle_url = chosen.get("src", "")
            subtitle_lang = chosen.get("lang", "")

        master_text = self._get_text(master, referer=embed_origin)
        variants, _embedded_audio_url = self._parse_master(master, master_text)
        audio_tracks = self._parse_audio_renditions(master, master_text)

        headers = {
            "Referer": embed_origin,
            "Origin": embed_origin.rstrip("/"),
            "User-Agent": _DEFAULT_UA,
        }
        audio_lang = "jpn,ja" if self.MODE == "sub" else "eng,en"
        if self.MODE == "dub" and audio_tracks:
            preferred = next(
                (
                    track
                    for track in audio_tracks
                    if str(track.get("lang") or "").lower().startswith("en")
                ),
                None,
            )
            if preferred and preferred.get("lang"):
                audio_lang = f"{preferred['lang']},en"

        qualities: List[QualityOption] = []
        for height, bandwidth, url in variants:
            label = f"{height}p" if height else "Auto"
            qualities.append(
                QualityOption(
                    name=label,
                    server_key=f"zoko{height or 'auto'}",
                    style="info",
                    direct_url=url,
                    res=label,
                    headers=dict(headers),
                    audio_lang=audio_lang,
                    ext="mp4",
                    subtitle_url=subtitle_url,
                    subtitle_lang=subtitle_lang,
                    subtitles=subtitles if self.MODE == "sub" else [],
                    # HLS audio renditions are selected in-stream via --alang,
                    # never as a separate --audio-file.
                    audio_tracks=list(audio_tracks),
                    master_url=master,
                    bandwidth=int(bandwidth or 0),
                )
            )

        if not qualities:
            qualities.append(
                QualityOption(
                    name="Auto",
                    server_key="zokoauto",
                    style="info",
                    direct_url=master,
                    res="Auto",
                    headers=dict(headers),
                    audio_lang=audio_lang,
                    ext="mp4",
                    subtitle_url=subtitle_url,
                    subtitle_lang=subtitle_lang,
                    subtitles=subtitles if self.MODE == "sub" else [],
                    audio_tracks=list(audio_tracks),
                    master_url=master,
                )
            )

        return {
            "CurrentEpisode": {"zoko": qualities[0].direct_url},
            "Qualities": qualities,
            "Subtitles": subtitles if self.MODE == "sub" else [],
            "AudioTracks": audio_tracks,
            "Mode": self.MODE,
        }

    @staticmethod
    def _server_hash(page: str, mode: str) -> str:
        for tag in re.findall(r"<div\b[^>]*\bserver-item\b[^>]*>", page, re.DOTALL):
            if _attr(tag, "data-type").lower() != mode:
                continue
            if _attr(tag, "data-server-name").lower() != "zokoanime":
                continue
            return _attr(tag, "data-hash")
        return ""

    @staticmethod
    def _decode_hash(value: str) -> str:
        try:
            padding = "=" * (-len(value) % 4)
            return base64.b64decode(value + padding).decode("utf-8", "replace").strip()
        except Exception:
            return ""

    @staticmethod
    def _deobfuscate(blob: str) -> Optional[Dict[str, Any]]:
        try:
            padding = "=" * (-len(blob) % 4)
            decoded = base64.b64decode(blob + padding)
        except Exception:
            return None
        output = bytes(
            byte ^ _XOR_KEY[index % len(_XOR_KEY)] for index, byte in enumerate(decoded)
        )
        try:
            data = json.loads(output.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/"

    @staticmethod
    def _normalize_subtitles(value: Any) -> List[Dict[str, Any]]:
        subtitles = []
        for item in value or []:
            if not isinstance(item, dict):
                continue
            src = str(item.get("src") or "").strip()
            if not src.startswith("http"):
                continue
            subtitles.append(
                {
                    "lang": str(item.get("lang") or "").strip(),
                    "label": str(item.get("label") or item.get("lang") or "").strip(),
                    "src": src,
                    "default": bool(item.get("default")),
                }
            )
        return subtitles

    @staticmethod
    def _parse_master(master: str, playlist: str):
        """Return ([(height, bandwidth, url)], audio_url) from an HLS master."""
        if not playlist.strip().startswith("#EXTM3U"):
            return [], ""
        lines = [line.strip() for line in playlist.splitlines() if line.strip()]
        variants = []
        audio_url = ""
        index = 0
        while index < len(lines):
            line = lines[index]
            if line.startswith("#EXT-X-STREAM-INF:"):
                info = line.split(":", 1)[1]
                height = 0
                resolution = re.search(r"RESOLUTION=\d+x(\d+)", info, re.IGNORECASE)
                if resolution:
                    height = int(resolution.group(1))
                bandwidth = _int(re.search(r"BANDWIDTH=(\d+)", info, re.IGNORECASE).group(1)) if re.search(r"BANDWIDTH=(\d+)", info, re.IGNORECASE) else 0
                next_index = index + 1
                while next_index < len(lines) and lines[next_index].startswith("#"):
                    next_index += 1
                if next_index < len(lines) and "EXT-X-I-FRAME" not in line:
                    variants.append((height, bandwidth, urljoin(master, lines[next_index])))
                index = next_index
            elif line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line.upper():
                if "DEFAULT=YES" in line.upper():
                    uri = re.search(r'URI="([^"]+)"', line)
                    if uri:
                        audio_url = urljoin(master, uri.group(1))
            index += 1
        variants.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return variants, audio_url

    @staticmethod
    def _parse_audio_renditions(master: str, playlist: str) -> List[Dict[str, Any]]:
        """Return HLS audio renditions declared by EXT-X-MEDIA, in master order."""
        renditions: List[Dict[str, Any]] = []
        seen = set()
        for raw_line in (playlist or "").splitlines():
            line = raw_line.strip()
            if not line.startswith("#EXT-X-MEDIA:"):
                continue
            payload = line.split(":", 1)[1]
            attrs: Dict[str, str] = {}
            for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', payload):
                value = match.group(2).strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                attrs[match.group(1).upper()] = value
            if attrs.get("TYPE", "").upper() != "AUDIO":
                continue
            uri = attrs.get("URI", "").strip()
            if not uri:
                continue
            lang = attrs.get("LANGUAGE", "").strip()
            label = attrs.get("NAME", "").strip() or lang
            key = (lang, label, uri)
            if key in seen:
                continue
            seen.add(key)
            renditions.append(
                {
                    "lang": lang,
                    "label": label,
                    "group": attrs.get("GROUP-ID", "").strip(),
                    "src": urljoin(master, uri),
                    "default": attrs.get("DEFAULT", "").upper() == "YES",
                    "autoselect": attrs.get("AUTOSELECT", "").upper() == "YES",
                }
            )
        return renditions

    def get_episode_streams(self, episode: Episode) -> List[QualityOption]:
        anime_id = getattr(episode, "anime_id", "") or _slug(getattr(episode, "url", ""))
        if not anime_id:
            return []
        data = self.get_streaming_servers(anime_id, episode.number)
        return (data or {}).get("Qualities", [])

    # ------------------------------------------------------------------
    # Feeds / listings
    # ------------------------------------------------------------------

    def get_latest_episodes(self, limit: int = 20) -> List[Episode]:
        page = self._get_text(f"{self.BASE_URL}/home")
        section = self._section(page, "Latest Episode")
        items = self._parse_film_items(section)
        episodes = []
        for item in items[: max(1, int(limit or 20))]:
            ep_count = _int(item.episodes) or 1
            language = item.language if item.language in ("sub", "dub") else self.MODE
            episodes.append(
                Episode(
                    number=str(ep_count),
                    display_num=ep_count,
                    title=item.title_en,
                    thumbnail=item.thumbnail,
                    url=f"{self.BASE_URL}/watch/{item.id}?ep=latest",
                    anime_id=item.id,
                    language=language,
                    available_modes=[language],
                )
            )
        return episodes

    def get_pinned_anime(self, limit: int = 20) -> List[AnimeResult]:
        page = self._get_text(f"{self.BASE_URL}/home")
        section = self._section(page, "Top 10")
        results: List[AnimeResult] = []
        seen = set()
        for block in re.split(r'<li class="item-top"', section)[1:]:
            heading = re.search(r'<h3 class="film-name">(.*?)</h3>', block, re.DOTALL)
            if not heading:
                continue
            anchor = re.search(r"<a\b[^>]*>", heading.group(1), re.DOTALL)
            if not anchor:
                continue
            anime_id = _slug(_attr(anchor.group(0), "href"))
            if not anime_id or anime_id in seen:
                continue
            seen.add(anime_id)
            img = re.search(r'<img\b[^>]*class="[^"]*film-poster-img[^"]*"[^>]*>', block, re.DOTALL)
            poster = _absolute(_attr(img.group(0), "src"), self.BASE_URL) if img else ""
            results.append(
                AnimeResult(
                    id=anime_id,
                    title_en=_attr(anchor.group(0), "title") or _text(heading.group(1)),
                    title_jp=_attr(anchor.group(0), "data-jname"),
                    thumbnail=poster,
                    language=self.MODE,
                )
            )
            if len(results) >= max(1, int(limit or 20)):
                break
        return results or self.get_trending_anime(0, limit)

    def get_trending_anime(self, from_index: int = 0, limit: int = 20) -> List[AnimeResult]:
        return self._paged_films("/top-airing", from_index, limit)

    def get_top_rated_anime(
        self, from_index: int = 0, limit: int = 20, rate_tier: str = "9-10"
    ) -> List[AnimeResult]:
        return self._paged_films("/most-popular", from_index, limit)

    def get_movies(self, from_index: int = 0, limit: int = 20) -> List[AnimeResult]:
        return self._paged_films("/movie", from_index, limit)

    def get_genre_anime(
        self, genre_slug: str, from_index: int = 0, limit: int = 20
    ) -> List[AnimeResult]:
        slug = str(genre_slug or "").strip().strip("/").split("/")[-1]
        return self._paged_films(f"/genres/{quote(slug)}", from_index, limit)

    def get_studio_anime(
        self, studio_name: str, from_index: int = 0, limit: int = 20
    ) -> List[AnimeResult]:
        raw = str(studio_name or "").strip()
        slug = _slug(raw) if "/" in raw else raw.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
        return self._paged_films(f"/studios/{quote(slug)}", from_index, limit)

    def get_anime_list(
        self,
        filter_type: str = "",
        filter_data: str = "",
        anime_type: str = "SERIES",
        from_index: int = 0,
        limit: int = 30,
    ) -> List[AnimeResult]:
        kind = (filter_type or "").upper()
        if kind == "SEARCH":
            results = self.search_anime(filter_data)
            return results[from_index:from_index + limit]
        if kind == "GENRE":
            return self.get_genre_anime(filter_data, from_index, limit)
        if kind in ("STUDIOS", "STUDIO"):
            return self.get_studio_anime(filter_data, from_index, limit)
        if kind == "MOVIE":
            return self.get_movies(from_index, limit)
        if kind == "TOP_RATED":
            return self.get_top_rated_anime(from_index, limit)
        if kind == "TRENDING":
            return self.get_trending_anime(from_index, limit)
        if kind == "POPULAR":
            return self._paged_films("/most-popular", from_index, limit)
        if kind in ("LATEST", "RECENT"):
            return [self._episode_to_anime(ep) for ep in self.get_latest_episodes(limit)]
        return self._paged_films("/most-popular", from_index, limit)

    @staticmethod
    def _episode_to_anime(episode: Episode) -> AnimeResult:
        return AnimeResult(
            id=episode.anime_id,
            title_en=episode.title,
            thumbnail=episode.thumbnail,
            episodes=str(episode.display_num),
            language=episode.language or "sub",
        )


class HiAnimeDubProvider(HiAnimeProvider):
    """HiAnime English dubbed provider."""

    id = "hianime_dub"
    name = "HiAnime (English Dub)"
    description = "English dubbed anime with HLS quality and subtitle tracks"

    MODE = "dub"
    language = "en"
    audio_mode = "dub"
    language_mode = "en_dub"
