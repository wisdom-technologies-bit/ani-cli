# ani-api

Independent FastAPI service over replaceable source adapters. HiAnime is the catalogue
and identification source, ZokoAnime does stream extraction, delivery is HLS through
signed proxy URLs.

## Authentication

Every request needs an API key, including `/health`, `/docs`, `/openapi.json`,
`/metrics`, stream resolution, the HLS playlist/segment proxy and the subtitle proxy:

```bash
curl -H "X-API-Key: $ANI_API_KEYS" https://<host>/v1/anime/mal/21
```

The key is never accepted from a query string and never logged. Signed stream tokens
(`/v1/stream/<token>/...`, `/v1/subtitles/<token>.vtt`) are an *additional* mechanism
for the URLs handed to players, not a replacement: both are required.

`ANI_API_KEYS` is a comma-separated list. With no keys configured the service refuses
non-loopback traffic with `AUTH_NOT_CONFIGURED` rather than serving openly. Enforcement
happens in middleware above the router, so a future endpoint cannot forget it.

## Endpoints

Existing (unchanged shapes):

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/search?q=` | HiAnime search |
| GET | `/v1/anime/{anime_id}` | episode count for a HiAnime slug |
| GET | `/v1/anime/{anime_id}/episodes` | HiAnime episode ids |
| GET | `/v1/episodes/{episode_id}/servers` | server list with support flags |
| GET | `/v1/streams/{episode_id}` | resolve + segment proof |
| GET | `/v1/stream/{token}/master.m3u8` | rewritten master playlist |
| GET | `/v1/stream/{token}/media.m3u8` | media playlist |
| GET | `/v1/subtitles/{token}.vtt` | subtitle proxy |
| GET | `/health`, `/health/providers`, `/metrics`, `/v1/admin/*` | ops |

New, MyAnimeList based:

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/anime/mal/{mal_id}` | MAL id → HiAnime anime + parts + mapping provenance |
| GET | `/v1/anime/mal/{mal_id}/episodes` | MAL episode → HiAnime episode ids, with part ranges |
| GET | `/v1/streams/mal/{mal_id}/episode/{n}?audio=sub\|dub&quality=best` | full path to a proven stream |
| GET | `/v1/admin/mal` | mapping store state (rows, persistence, title source) |

A MAL stream response is the normal `StreamResponse` plus a `mal` block:

```json
"mal": {
  "mal_id": 21, "mal_episode": 1,
  "hianime_id": "one-piece-1", "episode_id": "1",
  "stream_verified": true, "verification": "provider_embed",
  "evidence": "embed /mal/21/1/sub on episode 1"
}
```

`stream_verified` is `true` only when the embed behind the bytes served declares the
same MAL id *and* episode number.

## How MAL mapping works

1. **Verification, not similarity.** The ZokoAnime embed URL HiAnime hands out for an
   episode is `https://zokoanime.video/stream/mal/<mal_id>/<mal_episode>/<audio>`.
   Decoding HiAnime's base64 `data-hash` therefore reveals the MAL id and episode number
   the provider is actually serving. That is the proof a mapping rests on.
2. **Candidates.** A MAL id alone contains no title, so titles come from AniList (a
   public, key-free API - not a MyAnimeList scrape). They are used *only* to query
   HiAnime search. Title similarity shortlists and orders candidates; it never decides
   the answer.
3. **Proof.** Each shortlisted candidate's first episode is resolved to its embed and
   the parsed MAL id is compared with the request. A mismatch means the candidate is
   discarded, however good the title looked.
4. **Persistence.** Verified rows are stored in SQLite (`ANI_API_MAPPING_DB`) and cached
   (`ANI_API_TTL_MAL_MAPPING`), so later requests reuse the mapping instead of searching
   upstream again. Only embeddings that proved an id are stored; there is no seed file
   and no manual editing path, so an unproven mapping cannot enter the store.
5. **Episodes.** HiAnime episode order plus the provider's MAL episode number for the
   first episode gives each part a MAL range. A part is one HiAnime slug; split cours
   become several parts with non-overlapping ranges (`show-part-1` MAL 1-12,
   `show-part-2` MAL 13-24), and a request for episode 15 lands on part 2.
6. **Never a silent wrong episode.** If two different HiAnime uploads both claim the same
   MAL episode, the mapping is reported as `MAL_MAPPING_AMBIGUOUS`. If nothing proves the
   id, `MAL_NOT_MAPPED`. Before any stream is returned, the embed for that exact episode
   is re-checked; on disagreement the API raises `MAL_EPISODE_MISMATCH` instead of
   serving the wrong episode.

Error codes: `MAL_NOT_FOUND` (404, unknown id), `MAL_NOT_MAPPED` (404, no HiAnime entry
proved it), `MAL_MAPPING_AMBIGUOUS` (409), `MAL_EPISODE_NOT_FOUND` (404),
`MAL_EPISODE_MISMATCH` (409), `MAL_TITLE_SOURCE_DISABLED` / `MAL_TITLE_SOURCE_UNAVAILABLE`
(503).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANI_API_KEYS` | empty | comma-separated API keys |
| `ANI_API_TOKEN_SECRET` | empty | HMAC secret for proxy tokens (set this) |
| `ANI_API_MAPPING_DB` | `mal_mappings.sqlite3` | SQLite mapping store path |
| `ANI_API_MAL_TITLE_SOURCE` | `anilist` | candidate title source; `off` disables discovery |
| `ANI_API_MAL_CANDIDATES` | `6` | HiAnime candidates verified per MAL id |
| `ANI_API_ANILIST_URL` | `https://graphql.anilist.co` | AniList endpoint |
| `ANI_API_REDIS_URL` | empty | Redis cache backend (in-process otherwise) |
| `ANI_API_METADATA_BASE` | `https://hianime.at` | metadata source |
| `ANI_API_TIMEOUT` | `12` | upstream timeout (seconds) |
| `ANI_API_RATE_LIMIT` | `60` | requests/minute per key |

## Railway deployment

The previous Railway setup created a virtualenv during the build
(`python -m venv /app/.venv && /app/.venv/bin/pip install .`) and started
`/app/.venv/bin/python`. Build steps do not carry their filesystem into the running
container, so the start command failed with
`/bin/bash: line 1: /app/.venv/bin/python: No such file or directory`.

The service now uses Railway's Python environment directly (`api/railway.json` records
it in the repo):

```
Root Directory:  /api
Build Command:   python -m pip install -r requirements.txt
Start Command:   python -m uvicorn ani_api.main:app --app-dir src --host 0.0.0.0 --port $PORT
```

Notes:

- `--app-dir src` puts `src/` on `sys.path`, so `ani_api.main:app` imports without
  installing the project. `pyproject.toml` is kept for local/editable installs and
  `pip install .` still works; nothing about the dependency architecture changed.
- `api/requirements.txt` mirrors `pyproject.toml`'s runtime dependencies
  (`fastapi`, `httpx`, `uvicorn`). Test-only and optional extras stay in `pyproject.toml`.
- No `healthcheckPath` is configured on purpose: `/health` requires an API key and
  Railway's healthcheck cannot send headers. Verify the service with an authenticated
  request instead.
- Required variables: `ANI_API_KEYS`, `ANI_API_TOKEN_SECRET`. `PORT` is injected by
  Railway. `ANI_API_MAPPING_DB` points into a mounted volume if you want mappings to
  survive redeploys; otherwise they are re-derived on demand.
- The container filesystem is ephemeral: the SQLite store rebuilds itself after a
  redeploy (each mapping costs one discovery round trip).

## Tests

```bash
python -m pytest -m "not network"   # offline: units, auth, mapping logic
python -m pytest -m network         # live: real HiAnime, ZokoAnime and segments
```

Offline tests use a fake metadata source and inspect decision logic only; they never
claim a real stream. Live tests fail (or report `PROVIDER_OBJECT_BLOCKED`) honestly when
the provider refuses an object.
