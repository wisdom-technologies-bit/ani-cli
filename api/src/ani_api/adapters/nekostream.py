"""Download links from AniKoto's mapper (mapper.nekostream.site).

AniKoto exposes no downloads itself; its episode page points at a mapper keyed by
MAL id, episode and the episode's own `data-timestamp`:

    GET /api/mal/{mal_id}/{ep}/{timestamp}
    -> {"Kiwi": {"sub": {"download": {"720p": "https://...", "1080p": "https://..."}}},
        "status": {...}}

The timestamp is mandatory: without it the mapper answers 400 "Missing required
parameters". No credential is involved; the response carries no tokens.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "https://mapper.nekostream.site/api/mal"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
REQUEST_TIMEOUT = 15


class NekostreamError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}{': ' + detail if detail else ''}")
        self.code = code


def downloads(mal_id: int, episode: int, timestamp: int | str | None) -> list[dict[str, str]]:
    """Return [{provider, audio, quality, url}] for one episode."""
    if not timestamp or str(timestamp) in {"0", ""}:
        raise NekostreamError("MAPPER_NO_TIMESTAMP")
    url = f"{BASE}/{mal_id}/{episode}/{timestamp}"
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Referer": "https://anikoto.cz/", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        raise NekostreamError("MAPPER_HTTP", str(exc.code)) from exc
    except Exception as exc:
        raise NekostreamError("MAPPER_NETWORK", type(exc).__name__) from exc
    if body.get("error"):
        raise NekostreamError("MAPPER_ERROR", str(body.get("message"))[:80])

    links: list[dict[str, str]] = []
    for provider, audios in body.items():
        if provider == "status" or not isinstance(audios, dict):
            continue
        for audio, payload in audios.items():
            if not isinstance(payload, dict):
                continue
            found = payload.get("download") or {}
            if not isinstance(found, dict):
                continue
            for quality, link in found.items():
                if isinstance(link, str) and link.startswith("https://"):
                    links.append({"provider": str(provider), "audio": str(audio),
                                  "quality": str(quality), "url": link})
    links.sort(key=lambda item: (item["audio"], item["provider"], item["quality"]))
    return links
