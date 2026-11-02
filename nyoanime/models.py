from dataclasses import dataclass, field
from typing import List, Dict, Any

@dataclass
class AnimeResult:
    id: str  # Anime slug, e.g. "death-note"
    title_en: str
    title_jp: str = ""
    type: str = "SERIES"
    episodes: str = "N/A"
    status: str = "N/A"
    genres: str = "N/A"
    mal_id: str = "0"
    relation_id: str = ""
    score: str = "N/A"
    rank: str = "N/A"
    popularity: str = "N/A"
    rating: str = "N/A"
    premiered: str = "N/A"
    creators: str = "N/A"
    duration: str = "N/A"
    thumbnail: str = ""
    title_romaji: str = ""
    trailer: str = ""
    yt_trailer: str = ""
    # Enhanced rich metadata fields
    title_ar: str = ""
    synopsis: str = ""
    studio: str = ""
    author: str = ""
    batch_download_url: str = ""
    other_names: List[str] = field(default_factory=list)
    external_links: Dict[str, str] = field(default_factory=dict)
    trailers: List[str] = field(default_factory=list)
    # Stardima / multi-language metadata
    language: str = ""  # "dub" | "sub" | ""
    content_type: str = ""  # "anime" | "cartoon" | "live_action" | "unknown"

@dataclass
class Episode:
    number: str
    type: str = "Episode"
    display_num: int = 1
    # Enhanced rich episode fields
    title: str = ""  # Arabic episode title, e.g. "أنا معتاد على ذلك"
    duration: str = ""  # e.g. "23:44"
    thumbnail: str = ""
    is_last: bool = False
    url: str = ""
    servers: List[Dict[str, Any]] = field(default_factory=list)
    anime_id: str = ""
    # Language availability for the episode, e.g. "sub", "dub", "ar_sub".
    language: str = ""
    available_modes: List[str] = field(default_factory=list)

@dataclass
class QualityOption:
    name: str  # e.g. "FHD • 1080p (Full HD)"
    server_key: str = "1080p"  # e.g. "1080p"
    style: str = "info"
    # Direct stream URL & resolution info
    direct_url: str = ""
    res: str = ""
    premium: bool = False
    headers: Dict[str, str] = field(default_factory=dict)
    audio_lang: str = ""  # mpv --alang preference, e.g. "ara,ar"
    ext: str = "mp4"  # container extension for downloads
    # Optional sidecar tracks for providers that expose them separately.
    subtitle_url: str = ""
    subtitle_lang: str = ""
    subtitles: List[Dict[str, Any]] = field(default_factory=list)
    audio_url: str = ""
    # HLS audio renditions (embedded in the master playlist, not sidecar files).
    audio_tracks: List[Dict[str, Any]] = field(default_factory=list)
    # Master playlist when variants/audio renditions must be selected by the player.
    master_url: str = ""
    # Selected variant bandwidth, used by mpv --hls-bitrate when playing a master.
    bandwidth: int = 0

    @property
    def url(self) -> str:
        return self.direct_url

    @url.setter
    def url(self, val: str):
        self.direct_url = val

    @property
    def resolution(self) -> str:
        return self.res or self.server_key

    @property
    def server(self) -> str:
        return self.name
