"""ZokoAnime stream adapter.

The embed ships its config as base64(json XOR "otaku-embed-v1"). The shell version
also re-encodes each byte through an octal-safe transform before printf; in Python
that transform is an identity round-trip, so decode is base64 then XOR.

Verified live: the decoded JSON carries "src" (HLS master) and "subtitles".
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import urlsplit

import httpx

from ..config import settings
from ..errors import embed_decode_failed, no_stream, upstream_blocked
from ..http_client import LoopClient
from ..models import SubtitleTrack
from ..services.subtitles import non_subtitle_url
from .base import StreamSet
from .playlist import parse_audio_tracks, parse_master

KEY = b"otaku-embed-v1"
_BLOB = re.compile(r'window\.__P="([^"]+)"')


def deobfuscate(blob: str) -> bytes:
    try:
        raw = base64.b64decode(blob + "=" * (-len(blob) % 4))
    except (binascii.Error, ValueError) as exc:
        raise embed_decode_failed(f"base64: {exc}") from exc
    return bytes(b ^ KEY[i % len(KEY)] for i, b in enumerate(raw))


class ZokoAnimeAdapter:
    name = "zokoanime"
    supports = {"zokoanime"}

    def __init__(self) -> None:
        self._http = LoopClient()

    async def resolve(self, embed_url: str, referer: str | None = None) -> StreamSet:
        parts = urlsplit(embed_url)
        referer = referer or f"{parts.scheme}://{parts.netloc}/"
        r = await self._http.client.get(embed_url, headers={"Referer": referer})
        if r.status_code >= 400 or "Just a moment" in r.text[:4000]:
            raise upstream_blocked("embed", f"HTTP {r.status_code}")
        match = _BLOB.search(r.text)
        if not match:
            raise embed_decode_failed("window.__P not found")
        payload = json.loads(deobfuscate(match.group(1)).decode("utf-8", "replace").rstrip("\x00"))

        master = payload.get("src") or ""
        if not master:
            raise no_stream("embed payload has no src")
        # Keep every track, in the provider's order. The provider's own label and
        # lang fields are preserved verbatim; language handling happens later, where
        # the label is parsed. Nothing is dropped for being non-English: an episode
        # that lists one track yields one and an episode that lists ten yields ten.
        sprite = payload.get("sprite_vtt") or ""
        subs: list[SubtitleTrack] = []
        for entry in payload.get("subtitles") or []:
            if not isinstance(entry, dict):
                continue
            src = entry.get("src")
            if not isinstance(src, str) or not src.strip():
                continue                       # no URL means no playable track
            src = src.strip()
            if src == sprite or non_subtitle_url(src):
                continue                       # thumbnail sprite, not a subtitle
            label = entry.get("label")
            provider_lang = entry.get("lang")
            subs.append(SubtitleTrack(
                url=src,
                label=label.strip() if isinstance(label, str) and label.strip() else None,
                provider_lang=(provider_lang.strip()
                               if isinstance(provider_lang, str) and provider_lang.strip()
                               else None),
                default=bool(entry.get("default")),
            ))

        # the master playlist is only served to the embed origin
        pr = await self._http.client.get(master, headers={"Referer": referer})
        if pr.status_code >= 400:
            raise upstream_blocked("playlist", f"HTTP {pr.status_code}")
        variants = parse_master(pr.text, master)
        if not variants:
            raise no_stream("master playlist has no variants")
        audio = parse_audio_tracks(pr.text, master)
        return StreamSet(
            provider=self.name, master_url=master, referer=referer, variants=variants,
            subtitles=subs, audio_track_url=audio[0]["uri"] if audio else None,
            mal_id=(lambda m: m.group(1) if m else None)(re.search(r"/mal/(\d+)/", embed_url)),
        )

    async def aclose(self) -> None:
        await self._http.aclose()
