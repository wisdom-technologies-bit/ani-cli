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
import time
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
    ep_id: str          # data-ep-id the provider attached to this server


class AnikotoStream(TypedDict):
    master_url: str
    referer: str
    audio: str
    server: str
    episode_id: str     # provider episode id this file proved it belongs to
    file_id: str        # MegaPlay file id (identity of the media file)
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


_EMBED_ID_CACHE: dict[str, str] = {}


def embed_file_id(embed_url: str) -> str:
    """The file id the embed page declares, not the one in its own URL.

    AniKoto's embed path carries an id that MegaPlay has since re-pointed at a
    different file: /stream/s-2/2142/sub declares data-id="36396", and asking
    getSources for 2142 answers with another anime's package while 36396
    answers with the episode that was actually requested. The page is the
    authority, so it is read first and the URL id is only a fallback.
    """
    key = embed_url.split("?")[0]
    cached = _EMBED_ID_CACHE.get(key)
    if cached:
        return cached
    declared = ""
    try:
        html = _fetch(key, MEGAPLAY_REFERER, ajax=False)
        match = re.search(r'data-id=["\'](\d+)["\']', html)
        if match:
            declared = match.group(1)
    except Exception:
        declared = ""
    file_id = declared or file_id_from_embed(embed_url)
    _EMBED_ID_CACHE[key] = file_id
    return file_id


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
            file_id = embed_file_id(embed)
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


# --- catalogue cache ------------------------------------------------------
# Matching a title means inspecting several candidate catalogues, and a long
# runner's episode list is a ~600 KB ajax payload. Cache it briefly so ranking
# stays cheap without ever serving a stale-but-wrong mapping.
_CATALOG_TTL = 900
_CATALOG_CACHE: "dict[str, tuple[float, list[AnikotoEpisode]]]" = {}


def catalog(slug: str, refresh: bool = False) -> list[AnikotoEpisode]:
    """Episode rows for one slug, cached for a few minutes."""
    hit = _CATALOG_CACHE.get(slug)
    now = time.time()
    if hit and not refresh and now - hit[0] < _CATALOG_TTL:
        return hit[1]
    listing = episodes(slug)
    _CATALOG_CACHE[slug] = (now, listing)
    return listing


_CATALOGED = True


# --- episode pinning ------------------------------------------------------
# A servers token is only trustworthy if the episode id it carries is the
# episode we asked for. The episode list exposes data-id per episode; the server
# list echoes it back as data-ep-id on every <li>. Every listed server must
# carry the expected id - a list that merely *contains* it can still hand back a
# different episode's file, which is exactly how a wrong episode gets served.
_ANIME_ID_RE = re.compile(r'data-anime-id="(\d+)"')
_EP_PAIR_RE = re.compile(r'<a\b[^>]*data-num="(\d+)"[^>]*data-id="(\d+)"', re.S)
_EP_PAIR_RE_ALT = re.compile(r'<a\b[^>]*data-id="(\d+)"[^>]*data-num="(\d+)"', re.S)
_SERVER_EP_ID_RE = re.compile(r'data-ep-id="(\d+)"')
_SERVER_LI_RE = re.compile(r'<li\b([^>]*data-link-id[^>]*)>', re.S)

_EP_ID_CACHE: "dict[str, dict[str, str]]" = {}
_SERVER_LIST_CACHE: "dict[tuple[str, str], tuple[float, str]]" = {}


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


def _server_list_html(slug: str, episode: str) -> str:
    """Raw /ajax/server/list body for one episode, cached."""
    key = (slug, str(episode))
    hit = _SERVER_LIST_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _CATALOG_TTL:
        return hit[1]
    row = next((e for e in catalog(slug) if str(e["number"]) == str(episode)), None)
    html = ""
    if row is not None:
        try:
            body = _ajax(f"{BASE}/ajax/server/list?servers={row['token']}",
                         f"{BASE}/watch/{slug}/ep-{episode}")
            html = str(body.get("result") or "")
        except Exception:
            html = ""
    _SERVER_LIST_CACHE[key] = (now, html)
    return html


def _link_ep_ids(slug: str, episode: str) -> "dict[str, str]":
    """link-id -> data-ep-id for every server offered for this episode."""
    out: "dict[str, str]" = {}
    for attrs in _SERVER_LI_RE.findall(_server_list_html(slug, episode)):
        link = re.search(r'data-link-id="([^"]+)"', attrs)
        ep = _SERVER_EP_ID_RE.search(attrs)
        if link:
            out[link.group(1)] = ep.group(1) if ep else ""
    return out


def pin_ok(expected: str, served: "set[str]") -> bool:
    """True while some offered server provably belongs to this episode.

    Per-server filtering happens in `pinned_servers`, so a mixed list is not a
    failure by itself: what must hold is that at least one server carries the
    requested episode id. An empty signal cannot contradict the request.
    """
    if not expected or not served:
        return True
    return expected in served


_servers_impl = servers


def servers(slug: str, episode: str) -> list[AnikotoServer]:
    """Servers for one episode, each pinned to that episode's data-id."""
    expected = episode_id(slug, episode)
    links = _link_ep_ids(slug, episode)
    served = {value for value in links.values() if value}

    if expected and served and expected not in served:
        raise AnikotoError(
            "ANIKOTO_EPISODE_MISMATCH",
            f"episode {episode} token served ep ids {sorted(served)}, expected {expected}")
    if expected and not served and links:
        raise AnikotoError(
            "ANIKOTO_EPISODE_UNPINNED",
            f"server list for episode {episode} carries no data-ep-id to prove it")

    entries = _servers_impl(slug, episode)
    return [AnikotoServer(**{**entry, "ep_id": links.get(entry["link_id"], "")})
            for entry in entries]


def pinned_servers(slug: str, episode: str, audio: str) -> list[AnikotoServer]:
    """Servers for one episode and audio, refusing any that prove a different episode."""
    expected = episode_id(slug, episode)
    out = [entry for entry in servers(slug, episode)
           if entry["audio"] == audio and (not expected or entry.get("ep_id") in ("", expected))]
    if not out:
        raise AnikotoError("ANIKOTO_NO_SERVERS", audio)
    return out


def stream(slug: str, episode: str, audio: str = "sub", server_name: str | None = None) -> AnikotoStream:
    """Resolve one episode, proving every server used belongs to that episode."""
    expected = episode_id(slug, episode)
    pinned = pinned_servers(slug, episode, audio)
    if server_name:
        pinned.sort(key=lambda entry: entry["name"].lower() != server_name.lower())

    referer = f"{BASE}/watch/{slug}/ep-{episode}"
    errors: list[str] = []
    for option in pinned:
        try:
            resolved = _ajax(f"{BASE}/ajax/server?get={option['link_id']}", referer)
            embed = (resolved.get("result") or {}).get("url") or ""
            if "megaplay." not in embed:
                errors.append(f"{option['name']}: not megaplay")
                continue
            file_id = embed_file_id(embed)
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
                "episode_id": option.get("ep_id") or expected,
                "file_id": file_id,
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


def subtitle_text(url: str) -> str:
    """Body of a caption track the package ships; these name the show it belongs to."""
    return _fetch(url, MEGAPLAY_REFERER, ajax=False)


def assert_not_duplicate_dub(slug: str, episode: str, resolved: AnikotoStream) -> None:
    """Refuse a dub whose file id is the sub's file id.

    MegaPlay keys getSources by file id alone, so /sub and /dub in the embed URL
    are cosmetic and both decrypt to one file. Where that happens a dub request
    must fail loudly rather than hand back the sub audio.
    """
    if resolved.get("audio") != "dub":
        return
    dub_id = resolved.get("file_id") or ""
    sub = next((entry for entry in servers(slug, episode) if entry["audio"] == "sub"), None)
    if not sub:
        return
    try:
        body = _ajax(f"{BASE}/ajax/server?get={sub['link_id']}", f"{BASE}/watch/{slug}/ep-{episode}")
        match = _EMBED_FID.search(str((body.get("result") or {}).get("url") or ""))
        sub_id = match.group(1) if match else ""
    except Exception:
        return
    if dub_id and sub_id and dub_id == sub_id:
        raise AnikotoError(
            "ANIKOTO_DUB_DUPLICATE",
            f"dub shares file id {dub_id} with sub; refusing to serve sub audio as dub")


_EPISODE_PINNED = True
