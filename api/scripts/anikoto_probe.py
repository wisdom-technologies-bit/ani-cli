"""Live proof for the AniKoto -> MegaPlay chain.

    python3 api/scripts/anikoto_probe.py [query] [episode]

Prints only public facts: slug, server, master playlist host, variant count.
"""
from __future__ import annotations

import sys
import urllib.request

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0] + "/src")
from ani_api.adapters.anikoto import episodes, search, servers, stream  # noqa: E402


def playlist_ok(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": "OtakuNaijaPlay", "Referer": "https://megaplay.buzz/"})
    with urllib.request.urlopen(request, timeout=25) as response:
        body = response.read(2048).decode("utf-8", "ignore")
        return f"HTTP {response.status} {'playlist' if body.startswith('#EXTM3U') else body[:40]!r} variants={body.count('#EXT-X-STREAM-INF')}"


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "one piece"
    episode = sys.argv[2] if len(sys.argv) > 2 else "1"
    results = search(query, limit=3)
    print("search:", [(r["slug"], r["title"]) for r in results])
    if not results:
        return 1
    slug = results[0]["slug"]
    listing = episodes(slug)
    target = next((e for e in listing if e["number"] == episode), listing[0])
    print(f"slug={slug} episodes={len(listing)} ep={target['number']} mal_id={target['mal_id']}")
    print("servers:", [(s["name"], s["audio"]) for s in servers(slug, target["number"])])
    for audio in ("sub", "dub"):
        try:
            resolved = stream(slug, target["number"], audio=audio)
        except Exception as exc:
            print(f" {audio}: failed -> {exc}")
            continue
        print(f" {audio}: server={resolved['server']} master={resolved['master_url'][:60]}")
        print(f"   subtitles={[s['label'] for s in resolved['subtitles']]} intro={resolved['intro']} outro={resolved['outro']}")
        print("   ", playlist_ok(resolved["master_url"]))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
