# ani-api route reference

Base URL: `https://ani-api.kumoritv.top` (local: `http://127.0.0.1:8000`).

Every route requires the key header, including `/health`, `/docs` and the token
routes: `X-API-Key: <key>`. Keys live in `ANI_API_KEYS` (comma separated, several
allowed); with none configured the service refuses non-loopback traffic with
`503 AUTH_NOT_CONFIGURED`. The key is never accepted from a query string.

## Source ordering

`/v1/streams/mal/...` tries **AniKoto first** (`provider: "anikoto"`, MegaPlay) and
falls back to **ZokoAnime** (`provider: "zokoanime"`) on any problem, unchanged.

| Switch | Default | Meaning |
|---|---|---|
| `ANIKOTO_ENABLED` | `1` | Try AniKoto first on the MAL stream route. `0` restores previous behaviour exactly. |
| `ANIKOTO_DUB_ENABLED` | `1` | Allow AniKoto on the dub path. Audio language is provider-declared, never verified. |

AniKoto failures never surface as errors: the request falls through to ZokoAnime,
and only a total failure produces `NO_WORKING_STREAM`.

## Discovery

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Liveness. |
| GET | `/health/providers` | Stage-by-stage upstream health. |
| GET | `/v1/search?q=` | HiAnime catalogue search. `[{id, title}]`. |

## MAL addressable (preferred)

| Method | Path | Query | Response |
|---|---|---|---|
| GET | `/v1/anime/mal/{mal_id}` | `refresh` | `{mal_id, title, episode_count}` |
| GET | `/v1/anime/mal/{mal_id}/episodes` | `refresh` | `[{id, number}]` |
| GET | `/v1/streams/mal/{mal_id}/episode/{n}` | `audio=sub\|dub`, `quality=best`, `refresh` | `StreamResponse` |
| GET | `/v1/downloads/mal/{mal_id}/episode/{n}` | none | `DownloadResponse` |

## HiAnime id addressable (legacy, unchanged)

| Method | Path | Query |
|---|---|---|
| GET | `/v1/anime/{anime_id}` | none |
| GET | `/v1/anime/{anime_id}/episodes` | none |
| GET | `/v1/episodes/{episode_id}/servers` | `audio` |
| GET | `/v1/streams/{episode_id}` | `audio`, `quality`, `anime_id`, `refresh`, `server` |

## Signed playback routes (token is the credential)

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/stream/{token}/master.m3u8` | rewritten master, every URI absolute to this API |
| GET | `/v1/stream/{token}/media.m3u8` | media playlist |
| GET | `/v1/subtitles/{token}.vtt` | one track, WebVTT |

## Operations

| Method | Path |
|---|---|
| GET | `/metrics` |
| GET | `/v1/admin/providers` |
| GET | `/v1/admin/mal` |
| GET | `/v1/admin/lifetime` |

## StreamResponse fields clients use

`provider`, `audio.{type,declared,verified}`, `quality_selected`, `stream_type`,
`master_url`, `proxied_master_url`, `headers_required.{master_playlist,subtitles,segments}`,
`variants[].{quality,playable,first_segment_url}`, `subtitles[].{label,language,kind,format,default,url,proxied_url}`,
`intro`, `outro`, `download`, `validation`, `url_lifetime`, `mal`.

`intro`/`outro` are seconds, populated only when the source supplies them (AniKoto
does, ZokoAnime returns `null`).

## DownloadResponse

```json
{ "mal_id": 21, "episode_number": 1, "available": true, "provider": "nekostream",
  "reason": null,
  "sources": [{"provider":"Kiwi","audio":"sub","quality":"1080p","url":"https://pahe.nekostream.site/fmkoo"}],
  "note": "external download hosts; this API does not mirror or proxy the files" }
```

`404 ANIKOTO_NOT_MAPPED` = no AniKoto mapping for that MAL id. `200` with
`available: false` + `reason` = mapped but no links; hide the button.

## Wiring a download button (KumoriTV)

```ts
const res = await fetch(`${ANI_API_BASE}/v1/downloads/mal/${malId}/episode/${ep}`, {
  headers: { 'X-API-Key': process.env.ANI_API_KEY! },   // server-side only
})
if (!res.ok) return null                 // 404 = no mapping
const data = await res.json()
if (!data.available) return null         // button stays hidden
const byAudio = Object.groupBy(data.sources, (s) => s.audio)
```

`url` is a landing page on the download host, not a file: open in a new tab
(`target="_blank" rel="noreferrer"`). Never call this route from the browser.

## Latency observed (local, 2026-11-15, AniKoto first)

cold resolve ~1.2-3.0 s (AniKoto's own ~1.1 s), warm ~0.8 s, ZokoAnime fallback
~0.9-1.4 s.
