"""Error taxonomy. Every code maps to a failure mode observed in testing."""
from fastapi import HTTPException


class AniError(HTTPException):
    def __init__(self, status: int, code: str, title: str, *, stage: str = "", provider: str = "",
                 retryable: bool = False, retry_after: int | None = None, detail: str = ""):
        super().__init__(status_code=status, detail={
            "code": code, "title": title, "stage": stage, "provider": provider,
            "retryable": retryable, "retry_after": retry_after, "detail": detail,
        })


def no_results(q: str) -> AniError:
    return AniError(404, "NO_RESULTS", "No results", stage="search", detail=q)


def upstream_blocked(stage: str, detail: str = "") -> AniError:
    # Cloudflare challenge or WAF page. Deliberately distinct from NO_RESULTS: the
    # shell script conflates these, which is how a blocked upstream looks like "no anime".
    return AniError(503, "UPSTREAM_BLOCKED", "Upstream blocked the request",
                    stage=stage, provider="hianime", retryable=True, retry_after=60, detail=detail)


def unsupported_server(name: str, reason: str) -> AniError:
    return AniError(422, "SERVER_LISTED_UNSUPPORTED", f"Server {name} is listed but unsupported",
                    stage="servers", provider=name, detail=reason)


def embed_decode_failed(detail: str = "") -> AniError:
    return AniError(502, "EMBED_DECODE_FAILED", "Could not decode provider embed payload",
                    stage="embed", provider="zokoanime", retryable=True, detail=detail)


def no_stream(reason: str) -> AniError:
    return AniError(502, "NO_STREAM", "No usable stream", stage="resolve", provider="zokoanime",
                    retryable=True, detail=reason)


def object_blocked_segment(*, quality: str, http_code: int, cached: bool) -> AniError:
    return AniError(409, "PROVIDER_OBJECT_BLOCKED",
                    "Provider blocks this video object",
                    stage="segment", provider="zokoanime", retryable=False,
                    detail=f"quality {quality}: first segment HTTP {http_code}"
                           + (" (edge-cached 403; retrying the same URL will keep failing)" if cached else ""))


# ---------- MAL id taxonomy ----------
# Each of these is a distinct, actionable outcome. None of them ever falls back to
# "closest title match": an unproven mapping is reported, never guessed.
def mal_not_found(mal_id: str) -> AniError:
    return AniError(404, "MAL_NOT_FOUND", "Unknown MAL id",
                    stage="mal_lookup", provider="anilist",
                    detail=f"MAL id {mal_id} is not a known anime entry")


def mal_title_source_disabled() -> AniError:
    return AniError(503, "MAL_TITLE_SOURCE_DISABLED",
                    "No title source configured for MAL discovery",
                    stage="mal_lookup", provider="config",
                    detail="ANI_API_MAL_TITLE_SOURCE is off and no stored mapping exists")


def mal_title_source_unavailable(detail: str = "") -> AniError:
    return AniError(503, "MAL_TITLE_SOURCE_UNAVAILABLE",
                    "MAL metadata lookup temporarily unavailable",
                    stage="mal_lookup", provider="anilist", retryable=True,
                    retry_after=30, detail=detail)


def mal_not_mapped(mal_id: str, tried: list[str], detail: str = "") -> AniError:
    return AniError(404, "MAL_NOT_MAPPED",
                    "No verified HiAnime mapping for this MAL id",
                    stage="mal_mapping", provider="hianime",
                    detail=(f"no HiAnime candidate proved MAL id {mal_id} via provider embed"
                            + (f"; candidates tried: {', '.join(tried)}" if tried else "")
                            + (f"; {detail}" if detail else "")))


def mal_ambiguous(mal_id: str, options: list[str]) -> AniError:
    return AniError(409, "MAL_MAPPING_AMBIGUOUS",
                    "Several HiAnime entries claim the same MAL id",
                    stage="mal_mapping", provider="hianime",
                    detail=("refusing to guess; overlapping verified options: "
                            + ", ".join(options)))


def mal_episode_not_found(mal_id: str, number: str, available: str) -> AniError:
    return AniError(404, "MAL_EPISODE_NOT_FOUND",
                    "Episode not present in the verified mapping",
                    stage="mal_episode", provider="hianime",
                    detail=f"MAL {mal_id} has no episode {number}; available: {available}")


def mal_episode_mismatch(mal_id: str, number: str, found: str) -> AniError:
    return AniError(409, "MAL_EPISODE_MISMATCH",
                    "Provider embed disagrees with the requested MAL episode",
                    stage="mal_episode", provider="zokoanime",
                    detail=(f"asked for MAL {mal_id} episode {number}, provider embed serves "
                            f"{found}; not returning a possibly wrong episode"))
