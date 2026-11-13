"""Response models. Fields we cannot verify are typed Optional and default to None/False."""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field

AudioType = Literal["sub", "dub"]


class AnimeRef(BaseModel):
    id: str
    title: str


class EpisodeRef(BaseModel):
    id: str
    number: str


class AnimeDetail(BaseModel):
    id: str
    title: Optional[str] = None
    episode_count: int
    detail_source: str = "episodes_api"


class ServerRef(BaseModel):
    name: str
    audio: AudioType
    supported: bool
    reason: Optional[str] = None


class SubtitleTrack(BaseModel):
    """One subtitle track, exactly as the provider offered it plus display metadata.

    ``label`` and ``provider_lang`` are the provider's own values, kept verbatim.
    ``language``/``language_name``/``title`` are derived from the label only (the
    provider's ``lang`` field is a constant ``"en"`` on every track observed, so it
    is preserved but never used as the track language). Track language is never
    verified against the audio: no speech recognition or language detection runs.
    """
    url: str
    id: str = ""
    index: int = 0
    label: Optional[str] = None
    title: str = ""
    language: str = "und"
    language_name: str = "Unknown Language"
    language_source: Literal["provider_label", "unknown"] = "unknown"
    kind: Literal["full", "signs", "forced", "dubtitle"] = "full"
    format: Literal["vtt", "srt", "ass", "unknown"] = "unknown"
    format_source: Literal["url_extension", "sniffed", "unknown"] = "unknown"
    format_served: Literal["vtt"] = "vtt"
    converted: bool = False
    playable: bool = False
    default: bool = False
    provider_lang: Optional[str] = None
    proxied_url: Optional[str] = None
    language_inferred: Optional[str] = None
    language_verified: bool = False


class Variant(BaseModel):
    quality: str                       # normalised label, e.g. "1080p"
    height: Optional[int] = None
    width: Optional[int] = None
    bandwidth_bps: Optional[int] = None
    playlist_url: str
    first_segment_url: Optional[str] = None
    playable: Optional[bool] = None    # None = not probed
    probe_http_code: Optional[int] = None
    probe_bytes: Optional[int] = None
    edge_cached: Optional[bool] = None
    blocked_reason: Optional[str] = None


class Validation(BaseModel):
    """Proof that we actually delivered video, not just that a URL existed."""
    first_segment_checked: bool
    segment_http_code: Optional[int] = None
    segment_bytes: Optional[int] = None
    segment_container: Optional[str] = None
    edge_cache_status: Optional[str] = None
    cached_403_detected: bool = False
    checked_at: Optional[str] = None


class AudioInfo(BaseModel):
    type: AudioType
    declared: AudioType
    detected_language: Optional[str] = None
    verified: bool = False
    note: Optional[str] = None


class UrlLifetime(BaseModel):
    extracted_at: Optional[str] = None
    expires_at: Optional[str] = None      # only ever set from a measured observation
    expiry_known: bool = False
    observed_valid_for_minutes: Optional[int] = None
    note: Optional[str] = None


class DownloadInfo(BaseModel):
    available: bool = False
    reason: Optional[str] = None


class MalPart(BaseModel):
    """One verified HiAnime entry that serves a range of MAL episodes.

    A MAL entry is normally one HiAnime slug; split cours are the exception, which
    is why parts exist instead of a single id.
    """
    hianime_id: str
    title: Optional[str] = None
    episode_count: int = 0
    mal_episode_start: Optional[int] = None
    mal_episode_end: Optional[int] = None
    verified_at: Optional[str] = None
    evidence: Optional[str] = None


class MalMapping(BaseModel):
    mal_id: int
    hianime_id: str
    title: Optional[str] = None
    title_english: Optional[str] = None
    title_native: Optional[str] = None
    titles: list[str] = Field(default_factory=list)
    format: Optional[str] = None
    season_year: Optional[int] = None
    episode_count: int = 0
    parts: list[MalPart] = Field(default_factory=list)
    verified: bool = False
    verified_by: Optional[str] = None      # e.g. "zokoanime_embed"
    verified_at: Optional[str] = None
    candidate_source: Optional[str] = None  # e.g. "anilist"
    reused: bool = False                    # served from stored mapping / cache
    note: Optional[str] = None


class MalAnimeDetail(BaseModel):
    mal_id: int
    hianime_id: str
    title: Optional[str] = None
    title_english: Optional[str] = None
    title_native: Optional[str] = None
    titles: list[str] = Field(default_factory=list)
    format: Optional[str] = None
    season_year: Optional[int] = None
    episode_count: int
    parts: list[MalPart] = Field(default_factory=list)
    mapping: MalMapping


class MalEpisodeRef(BaseModel):
    mal_id: int
    hianime_id: str
    episode_id: str
    number: str                              # HiAnime episode number
    mal_number: Optional[int] = None         # MAL episode number (part offset applied)
    title: Optional[str] = None
    title_source: Optional[str] = None
    part: Optional[MalPart] = None
    episode_verified: bool = False            # provider embed proved this exact mapping


class MalContext(BaseModel):
    mal_id: int
    mal_episode: int
    hianime_id: str
    episode_id: str
    part: Optional[MalPart] = None
    stream_verified: bool = False
    verification: str = "unverified"
    evidence: Optional[str] = None
    note: Optional[str] = None


class StreamResponse(BaseModel):
    request_id: str
    provider: str
    metadata_source: str
    anime_id: str
    episode_id: str
    episode_number: Optional[str] = None
    audio: AudioInfo
    quality_requested: str
    quality_selected: Optional[str] = None
    stream_type: Literal["hls"] = "hls"
    master_url: Optional[str] = None
    proxied_master_url: Optional[str] = None
    variants: list[Variant] = Field(default_factory=list)
    alternatives: list[Variant] = Field(default_factory=list)
    subtitles: list[SubtitleTrack] = Field(default_factory=list)
    subtitle_count: int = 0
    subtitle_default_id: Optional[str] = None
    audio_tracks: list[dict] = Field(default_factory=list)
    headers_required: dict = Field(default_factory=dict)
    validation: Validation
    url_lifetime: UrlLifetime
    download: DownloadInfo = Field(default_factory=DownloadInfo)
    playback_hint: Optional[str] = None
    cache: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    mal: Optional[MalContext] = None
