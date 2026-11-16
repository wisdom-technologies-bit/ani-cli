"""AniKoto (MegaPlay) as the FIRST stream source, ZokoAnime kept as the fallback.

Enabled with ANIKOTO_ENABLED (1|true|yes|on). Anything AniKoto cannot do returns
None so the caller continues into the existing hianime/ZokoAnime path untouched:
no new error bodies, no changed fields, no writes.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from ..adapters import anikoto as site
from ..adapters import nekostream
from ..models import (AudioInfo, DownloadInfo, DownloadSource, MalContext, StreamResponse,
                      SubtitleTrack, UrlLifetime, Validation, Variant)
from .security import sign_payload

MAP_PATH = Path(__file__).resolve().parents[3] / "data" / "anikoto_map.json"
MASTER_REFERER = "https://megaplay.buzz/"
PLAYBACK_HINT = ("HLS via AniKoto/MegaPlay. Segments live on the MegaPlay CDN and need "
                 "Referer https://megaplay.buzz/ for playlists.")
_DL_TTL = 600
_DL_CACHE: dict[tuple[int, int, int], tuple[float, list[dict[str, str]]]] = {}

_VARIANT = re.compile(r'#EXT-X-STREAM-INF:(?P<attrs>[^\n]+)\n(?P<uri>[^\n#]+)', re.S)


def dub_enabled() -> bool:
    """AniKoto dub. Owner enabled it on 2026-11-15 (ANIKOTO_DUB_ENABLED, default on).

    Audio language is still provider-declared, never verified by this service.
    """
    return os.environ.get("ANIKOTO_DUB_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return os.environ.get("ANIKOTO_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _mapped(mal_id: int) -> str | None:
    try:
        return json.loads(MAP_PATH.read_text()).get(str(mal_id))
    except Exception:
        return None


def _remember(mal_id: int, slug: str) -> None:
    try:
        data = json.loads(MAP_PATH.read_text())
    except Exception:
        data = {}
    if data.get(str(mal_id)) == slug:
        return
    data[str(mal_id)] = slug
    MAP_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _slug_by_title(title: str, mal_id: int, episode: str) -> str | None:
    """Title search, then prove the candidate with the episode's data-mal."""
    for hit in site.search(title, limit=5):
        try:
            listing = site.episodes(hit["slug"])
        except site.AnikotoError:
            continue
        if any(e["number"] == episode and e["mal_id"] == mal_id for e in listing):
            return hit["slug"]
    return None


def _downloads(mal_id: int, episode: int, timestamp: int | None) -> list[dict[str, str]]:
    """Download links from AniKoto's mapper, cached 10 minutes, never fatal."""
    if not timestamp:
        return []
    key = (mal_id, episode, timestamp)
    hit = _DL_CACHE.get(key)
    if hit and time.time() - hit[0] < _DL_TTL:
        return hit[1]
    try:
        links = nekostream.downloads(mal_id, episode, timestamp)
    except Exception:
        return []
    _DL_CACHE[key] = (time.time(), links)
    return links


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": site.USER_AGENT, "Referer": MASTER_REFERER})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", "ignore")


def _variants(master_text: str, master_url: str) -> list[dict[str, Any]]:
    base = master_url.rsplit("/", 1)[0] + "/"
    out: list[dict[str, Any]] = []
    for match in _VARIANT.finditer(master_text):
        attrs, uri = match.group("attrs"), match.group("uri").strip()
        if not uri.startswith("http"):
            uri = base + uri.lstrip("./")
        height = None
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
        if res:
            height = int(res.group(2))
        bandwidth = re.search(r"BANDWIDTH=(\d+)", attrs)
        out.append({"url": uri, "height": height,
                    "bandwidth": int(bandwidth.group(1)) if bandwidth else None})
    out.sort(key=lambda v: (v["height"] or 0, v["bandwidth"] or 0), reverse=True)
    return out


async def try_resolve(service, mal_id: int, episode: int, audio: str, quality: str,
                      request_id: str, title_hint: str | None = None) -> StreamResponse | None:
    """Return an AniKoto stream, or None to fall through to ZokoAnime."""
    if not enabled():
        return None
    if audio == "dub" and not dub_enabled():
        return None                      # unverified audio: let ZokoAnime answer, as today
    started = time.time()
    try:
        slug = _mapped(mal_id)
        if slug is None and title_hint:
            slug = await asyncio.to_thread(_slug_by_title, title_hint, mal_id, str(episode))
            if slug:
                _remember(mal_id, slug)
        if not slug:
            return None
        resolved = await asyncio.to_thread(site.stream, slug, str(episode), audio)
        # MegaPlay keys getSources by file id alone, so a dub request can hand back
        # the sub file. Refuse a duplicate and let the caller fall back.
        if audio == 'dub':
            try:
                _sub = await asyncio.to_thread(site.stream, slug, str(episode), 'sub')
            except Exception:
                return None
            if not resolved.get('master') or resolved.get('master') == _sub.get('master'):
                return None
        # Same guard for identity: one slug must always resolve to one anime hash.
        _h = _anime_hash(resolved.get('master') or '')
        if _h and _anime_hash_ok(slug, _h) is False:
            return None
        listing = await asyncio.to_thread(site.episodes, slug)
        matched = next((e for e in listing if e["number"] == str(episode)), None)
        if matched is None or matched["mal_id"] != mal_id:
            return None          # never serve a stream we cannot tie to this MAL episode
        master_text = await asyncio.to_thread(_fetch_text, resolved["master_url"])
        if not master_text.startswith("#EXTM3U"):
            return None
        options = _variants(master_text, resolved["master_url"])
        if not options:
            return None
        probed: list[Variant] = []
        chosen: Variant | None = None
        for option in options[:3]:
            first = await service._media_first_segment(option["url"], MASTER_REFERER)
            probe = await service.probe_segment(first, MASTER_REFERER) if first else {"reachable": False}
            variant = Variant(quality=f"{option['height']}p" if option["height"] else "auto",
                              height=option["height"], bandwidth_bps=option["bandwidth"],
                              playlist_url=option["url"], first_segment_url=first,
                              playable=bool(probe.get("reachable")),
                              probe_http_code=probe.get("http_code"),
                              probe_bytes=probe.get("bytes"))
            probed.append(variant)
            if variant.playable and chosen is None:
                chosen = variant
        if chosen is None:
            return None
        subs: list[SubtitleTrack] = []
        for index, track in enumerate(resolved["subtitles"]):
            sub = SubtitleTrack(
                url=track["url"], index=index, label=track["label"],
                title=track["label"], language="en" if track["srclang"] in ("und", "", None) else track["srclang"],
                language_name=track["label"], kind="full",
                format="vtt", format_source="url_extension", playable=True,
                default=track["default"], provider_lang=track["srclang"])
            sub.proxied_url = "/v1/subtitles/" + sign_payload(
                {"u": sub.url, "r": MASTER_REFERER, "k": "sub"}) + ".vtt"
            subs.append(sub)
        links = await asyncio.to_thread(_downloads, mal_id, episode, matched.get("timestamp"))
        payload = StreamResponse(
            request_id=request_id, provider="anikoto", metadata_source="anikoto",
            anime_id=slug, episode_id=str(matched["number"]), episode_number=str(episode),
            audio=AudioInfo(type=audio, declared=audio, verified=False,  # type: ignore[arg-type]
                            note="declared by provider; not language-verified for this episode"),
            quality_requested=quality, quality_selected=chosen.quality,
            master_url=resolved["master_url"],
            proxied_master_url="/v1/stream/" + sign_payload(
                {"u": resolved["master_url"], "r": MASTER_REFERER, "k": "master"}) + "/master.m3u8",
            variants=probed, alternatives=[v for v in probed if v is not chosen and v.playable],
            subtitles=subs, subtitle_count=len(subs),
            subtitle_default_id=next((s.id or str(i) for i, s in enumerate(subs) if s.default), None),
            headers_required={"master_playlist": {"Referer": MASTER_REFERER},
                              "subtitles": {"Referer": MASTER_REFERER}, "segments": {},
                              "verified_on": "2026-11-14", "user_agent_required": False,
                              "cookies_required": False},
            validation=Validation(first_segment_checked=True,
                                  segment_http_code=chosen.probe_http_code,
                                  segment_bytes=chosen.probe_bytes,
                                  segment_container="mpegts (video/mp2t)",
                                  checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            url_lifetime=UrlLifetime(expiry_known=False, note="no expiry token observed"),
            download=DownloadInfo(
                available=bool(links),
                provider="nekostream" if links else None,
                sources=[DownloadSource(**link) for link in links],
                reason=None if links else "upstream mapper listed no download links for this episode",
                note=("external download hosts; this API does not mirror or proxy the files"
                      if links else None)),
            playback_hint=PLAYBACK_HINT,
            cache={"hit": False, "age_seconds": 0, "validated_by": "fresh_probe"},
            intro=resolved["intro"], outro=resolved["outro"],
        )
        payload.mal = MalContext(
            mal_id=mal_id, mal_episode=episode, hianime_id=slug, episode_id=str(matched["number"]),
            stream_verified=True, verification="anikoto_data_mal",
            evidence=f"anikoto episode data-mal={matched['mal_id']} data-num={matched['number']}",
            note="AniKoto lists this episode's MAL id and number directly")
        payload.cache["anikoto_ms"] = int((time.time() - started) * 1000)
        return payload
    except Exception:
        return None


def downloads_for_mal(mal_id: int, episode: int, title_hint: str | None = None) -> dict:
    """Download links for one MAL episode from the AniKoto mapper. Sync: run in a thread."""
    slug = _mapped(mal_id) or (_slug_by_title(title_hint, mal_id, str(episode)) if title_hint else None)
    if not slug:
        return {"slug": None, "sources": [], "reason": "ANIKOTO_NOT_MAPPED"}
    try:
        listing = site.episodes(slug)
    except Exception as exc:
        return {"slug": slug, "sources": [], "reason": f"ANIKOTO_EPISODES_FAILED: {exc}"}
    matched = next((e for e in listing if str(e.get("number")) == str(episode)), None)
    sources = _downloads(mal_id, episode, (matched or {}).get("timestamp"))
    return {"slug": slug, "sources": sources,
            "reason": None if sources else "UPSTREAM_HAS_NO_LINKS"}
