"""AniKoto (anikoto.cz) source adapter.

AniKoto serves every stream through the MegaPlay player, so extraction is two
layers:

  site      GET /watch/<slug>/ep-<n>            -> data-anime-id
            GET /ajax/episode/list/<anime_id>   -> per-episode data-num + data-ids
                                                   (data-ids IS the `servers` token)
                                                   and data-mal (MAL id of the episode)
            GET /ajax/server/list?servers=<data-ids>
                                                -> <li data-sv-id data-link-id>NAME</li>
            GET /ajax/server?get=<data-link-id> -> {"url": <megaplay embed>, "skip_data"}
  megaplay  see adapters/megaplay.py            -> decrypted master.m3u8

No third-party dependency. No credentials: the site needs none, and MegaPlay's
key is the player's own public constant.
"""
from __future__ import annotations

import json
import re
from typing import Any, TypedDict
from urllib.parse import quote

from .megaplay import MegaPlayError, decrypt_enc, file_id_from_embed, master_url

BASE = "https://anikoto.cz"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)
MEGAPLAY_REFERER = "https://megaplay.buzz/"
REQUEST_TIMEOUT = 25

_AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
_EPISODE = re.compile(
    r'data-number="(?P<num>[^"]+)"|data-num="(?P<num2>[^"]+)"'
    r'[^>]*data-mal="(?P<mal>\d*)"[^>]*data-ids="(?P<ids>[^"]+)"',
    re.S,
)
_SERVER_GROUP = re.compile(r'data-type="(?P<audio>sub|dub)"(?P<body>.*?)(?=data-type="|$)', re.S)
_SERVER_ITEM = re.compile(
    r'data-sv-id="(?P<sv>[^"]+)"[^>]*data-link-id="(?P<link>[^"]+)"[^>]*>(?P<name>[^<]+)<', re.S
)
_ANCHOR = re.compile(r'<a\b[^>]*data-ids="[^"]+"[^>]*>', re.S)
_ATTR = re.compile(r'data-([a-z-]+)="([^"]*)"')
_ANIME_ID = re.compile(r'data-anime-id="(\d+)"')
_SEARCH_LINK = re.compile(
    r'<a class="name d-title" href="[^"]*/watch/(?P<slug>[^"/]+)[^"]*"[^>]*>(?P<title>[^<]*)<', re.S)
_SEARCH_FALLBACK = re.compile(r'href="[^"]*/watch/(?P<slug>[^"/]+)/ep-\d+"[^>]*>(?P<title2>[^<]{1,120})<', re.S)


class AnikotoError(RuntimeError):
    """AniKoto could not return a usable stream."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}{': ' + detail if detail else ''}")
        self.code = code
        self.detail = detail


class AnikotoEpisode(TypedDict):
    number: str
    mal_id: int | None
    token: str
    timestamp: int | None


class AnikotoServer(TypedDict):
    name: str
    audio: str
    server_id: str
    link_id: str


class AnikotoStream(TypedDict):
    master_url: str
    referer: str
    audio: str
    server: str
    subtitles: list[dict[str, Any]]
    intro: list[int] | None
    outro: list[int] | None


def _fetch(url: str, referer: str, ajax: bool = True) -> str:
    """GET a page or ajax endpoint. urllib only, so no extra dependency."""
    import urllib.error
    import urllib.request

    headers = {"User-Agent": USER_AGENT, "Referer": referer}
    if ajax:
        headers.update(_AJAX_HEADERS)
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        raise AnikotoError("ANIKOTO_UPSTREAM", f"HTTP {exc.code}") from exc
    except Exception as exc:  # network, timeout, TLS
        raise AnikotoError("ANIKOTO_NETWORK", str(exc)[:80]) from exc


def _ajax(url: str, referer: str) -> dict[str, Any]:
    body = _fetch(url, referer)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AnikotoError("ANIKOTO_BAD_JSON") from exc
    if payload.get("status") != 200:
        raise AnikotoError("ANIKOTO_BAD_REQUEST", str(payload.get("result"))[:60])
    return payload


def search(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Title search. AniKoto exposes no MAL lookup, so callers match by title."""
    page = _fetch(f"{BASE}/search?keyword={quote(query)}", BASE, ajax=False)
    seen: dict[str, dict[str, str]] = {}
    matches = list(_SEARCH_LINK.finditer(page)) or list(_SEARCH_FALLBACK.finditer(page))
    for match in matches:
        slug = match.group("slug").rstrip("/").split("/")[0]
        if not slug or slug in seen:
            continue
        title = match.groupdict().get("title") or match.groupdict().get("title2") or slug
        seen[slug] = {"slug": slug, "title": title.strip()}
        if len(seen) >= limit:
            break
    return list(seen.values())


def episodes(slug: str) -> list[AnikotoEpisode]:
    """Episode number -> (MAL id, `servers` token) for one slug."""
    page = _fetch(f"{BASE}/watch/{slug}", BASE, ajax=False)
    anime = _ANIME_ID.search(page)
    if not anime:
        raise AnikotoError("ANIKOTO_NO_ANIME_ID")
    listing = _ajax(f"{BASE}/ajax/episode/list/{anime.group(1)}", f"{BASE}/watch/{slug}")
    found: list[AnikotoEpisode] = []
    for anchor in _ANCHOR.finditer(listing["result"]):
        attrs = dict(_ATTR.findall(anchor.group(0)))
        token = attrs.get("ids")
        number = attrs.get("num") or attrs.get("number") or ""
        if not token or not number:
            continue
        timestamp = attrs.get("timestamp")
        found.append({
            "number": number,
            "mal_id": int(attrs["mal"]) if attrs.get("mal") else None,
            "token": token,
            "timestamp": int(timestamp) if timestamp and timestamp.isdigit() else None,
        })
    if not found:
        raise AnikotoError("ANIKOTO_NO_EPISODES")
    return found


def servers(slug: str, episode: str) -> list[AnikotoServer]:
    """Named servers for one episode, sub and dub."""
    match = next((e for e in episodes(slug) if e["number"] == str(episode)), None)
    if match is None:
        raise AnikotoError("ANIKOTO_EPISODE_NOT_FOUND", str(episode))
    referer = f"{BASE}/watch/{slug}/ep-{episode}"
    listing = _ajax(f"{BASE}/ajax/server/list?servers={match['token']}", referer)
    out: list[AnikotoServer] = []
    for group in _SERVER_GROUP.finditer(listing["result"]):
        for item in _SERVER_ITEM.finditer(group.group("body")):
            out.append({
                "name": item.group("name").strip(),
                "audio": group.group("audio"),
                "server_id": item.group("sv"),
                "link_id": item.group("link"),
            })
    if not out:
        raise AnikotoError("ANIKOTO_NO_SERVERS")
    return out


def stream(slug: str, episode: str, audio: str = "sub", server_name: str | None = None) -> AnikotoStream:
    """Resolve one episode to a decrypted master playlist."""
    referer = f"{BASE}/watch/{slug}/ep-{episode}"
    options = [s for s in servers(slug, episode) if s["audio"] == audio]
    if not options:
        raise AnikotoError("ANIKOTO_NO_SERVERS", audio)
    if server_name:
        options.sort(key=lambda s: s["name"].lower() != server_name.lower())
    errors: list[str] = []
    for option in options:
        try:
            resolved = _ajax(f"{BASE}/ajax/server?get={option['link_id']}", referer)
            embed = (resolved.get("result") or {}).get("url") or ""
            if "megaplay." not in embed:
                errors.append(f"{option['name']}: not megaplay")
                continue
            file_id = file_id_from_embed(embed)
            payload = json.loads(_fetch(
                f"https://megaplay.buzz/stream/getSources?id={file_id}", MEGAPLAY_REFERER))
            decrypted = decrypt_enc(payload["enc"])
            tracks = payload.get("tracks") or []
            skip = (resolved.get("result") or {}).get("skip_data") or {}
            return {
                "master_url": master_url(decrypted),
                "referer": MEGAPLAY_REFERER,
                "audio": audio,
                "server": option["name"],
                "subtitles": [
                    {"url": t.get("file"), "label": t.get("label") or "Undefined",
                     "srclang": (t.get("language") or "en"), "default": bool(t.get("default")),
                     "kind": t.get("kind") or "captions"}
                    for t in tracks if t.get("file") and not str(t.get("kind", "")).startswith("thumb")
                ],
                "intro": skip.get("intro"),
                "outro": skip.get("outro"),
            }
        except (AnikotoError, MegaPlayError, json.JSONDecodeError) as exc:
            errors.append(f"{option['name']}: {exc}")
    raise AnikotoError("ANIKOTO_ALL_SERVERS_FAILED", "; ".join(errors)[:200])

# --- dub duplicate guard -------------------------------------------------
# MegaPlay keys getSources by file id only, so /sub and /dub in the embed URL
# are cosmetic and both decrypt to one file. Verified live: one-piece-odmau and
# solo-leveling-ilh08 serve the SAME file id for sub and dub. Where that happens
# a dub request must fail loudly rather than hand back the sub audio.
_EMBED_FID = re.compile(r"/stream/[^/]+/(\d+)/")


def _embed_url(slug: str, episode: str, audio: str) -> str:
    for entry in servers(slug, str(episode)):
        if entry["audio"] == audio:
            body = _ajax(f"{BASE}/ajax/server?get={entry['link_id']}",
                         f"{BASE}/watch/{slug}/ep-{episode}")
            return str((body.get("result") or {}).get("url") or "")
    return ""


def _embed_fid(slug: str, episode: str, audio: str) -> str:
    match = _EMBED_FID.search(_embed_url(slug, episode, audio))
    return match.group(1) if match else ""


_stream_impl = stream


def stream(slug: str, episode: str, audio: str = "sub", server_name: str | None = None) -> AnikotoStream:
    result = _stream_impl(slug, episode, audio, server_name)
    if audio == "dub":
        dub_id = _embed_fid(slug, episode, "dub") or _EMBED_FID.search(result.get("master_url", "") or "") and ""
        sub_id = _embed_fid(slug, episode, "sub")
        if dub_id and sub_id and dub_id == sub_id:
            raise AnikotoError("ANIKOTO_DUB_DUPLICATE",
                               f"dub shares file id {dub_id} with sub; refusing to serve sub audio as dub")
    return result

_ANIKOTO_DUB_WRAPPED = True

# --- episode pinning ------------------------------------------------------
# A servers token is only trustworthy if the episode id it carries is the
# episode we asked for. The episode list exposes data-id per episode; the server
# list echoes it back as data-ep-id. They must agree, otherwise the token came
# from a different episode and the stream would be the wrong episode.
_ANIME_ID_RE = re.compile(r'data-anime-id="(\d+)"')
_EP_PAIR_RE = re.compile(r'<a\b[^>]*data-num="(\d+)"[^>]*data-id="(\d+)"', re.S)
_EP_PAIR_RE_ALT = re.compile(r'<a\b[^>]*data-id="(\d+)"[^>]*data-num="(\d+)"', re.S)
_SERVER_EP_ID_RE = re.compile(r'data-ep-id="(\d+)"')

_EP_ID_CACHE: "dict[str, dict[str, str]]" = {}
_SERVER_EP_CACHE: "dict[tuple[str, str], set[str]]" = {}


def _data_id_map(slug: str) -> "dict[str, str]":
    """episode number -> data-id, from the episode list. Cached per slug."""
    if slug in _EP_ID_CACHE:
        return _EP_ID_CACHE[slug]
    out: "dict[str, str]" = {}
    try:
        page = _fetch(f"{BASE}/watch/{slug}/ep-1", f"{BASE}/", ajax=False)
        found = _ANIME_ID_RE.search(page)
        if found:
            body = _ajax(f"{BASE}/ajax/episode/list/{found.group(1)}", f"{BASE}/watch/{slug}/ep-1")
            html = str(body.get("result") or "")
            for num, eid in _EP_PAIR_RE.findall(html):
                out[num] = eid
            if not out:
                for eid, num in _EP_PAIR_RE_ALT.findall(html):
                    out[num] = eid
    except Exception:
        out = {}
    _EP_ID_CACHE[slug] = out
    return out


def episode_id(slug: str, episode: str) -> str:
    """The provider's own id for one episode, '' when unknown."""
    return _data_id_map(slug).get(str(episode), "")


def _server_ep_ids(slug: str, episode: str) -> "set[str]":
    key = (slug, str(episode))
    if key in _SERVER_EP_CACHE:
        return _SERVER_EP_CACHE[key]
    out: "set[str]" = set()
    row = next((e for e in episodes(slug) if str(e["number"]) == str(episode)), None)
    if row is not None:
        try:
            body = _ajax(f"{BASE}/ajax/server/list?servers={row['token']}",
                         f"{BASE}/watch/{slug}/ep-{episode}")
            out = set(_SERVER_EP_ID_RE.findall(str(body.get("result") or "")))
        except Exception:
            out = set()
    _SERVER_EP_CACHE[key] = out
    return out


def pin_ok(expected: str, served: "set[str]") -> bool:
    """True unless the server list clearly belongs to another episode."""
    if not expected or not served:
        return True
    return expected in served


_servers_impl = servers


def servers(slug: str, episode: str) -> list[AnikotoServer]:
    """Servers for one episode, with the token pinned to that episode."""
    expected = episode_id(slug, episode)
    served = _server_ep_ids(slug, episode)
    if not pin_ok(expected, served):
        raise AnikotoError(
            "ANIKOTO_EPISODE_MISMATCH",
            f"episode {episode} token served ep ids {sorted(served)}, expected {expected}")
    return _servers_impl(slug, episode)


_EPISODE_PINNED = True
