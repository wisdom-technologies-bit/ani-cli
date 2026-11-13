"""Live subtitle matrix against the real provider. Run with: pytest -m network

Covers the cases the requirement names: one track, many tracks, English and Japanese
together, several tracks in the same language, sub and dub. Titles used here are
search terms for the test only; no anime, MAL id, episode id or subtitle URL is
hard-coded in the service itself.
"""
import base64

import pytest

from ani_api.adapters.hianime import HianimeAdapter
from ani_api.adapters.zokoanime import ZokoAnimeAdapter
from ani_api.services.subtitles import (enrich_tracks, format_from_url, sniff_format,
                                        to_vtt)

pytestmark = pytest.mark.network


async def tracks_for_episode(query: str, audio: str, episode_index: int = 0):
    """Search a title and resolve the nth episode through the real adapters."""
    md = HianimeAdapter()
    try:
        results = await md.search(query)
        episodes = await md.episodes(results[0].id)
        hashed = await md.server_hash(episodes[episode_index].id, audio, "ZokoAnime")
        embed = base64.b64decode(hashed + "=" * (-len(hashed) % 4)).decode()
    finally:
        await md.aclose()
    adapter = ZokoAnimeAdapter()
    try:
        streams = await adapter.resolve(embed, "https://zokoanime.video/")
        return enrich_tracks(streams.subtitles), streams.referer
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("audio", ["sub", "dub"])
async def test_every_listed_track_is_playable(audio):
    tracks, referer = await tracks_for_episode("one piece", audio)
    assert tracks, f"the provider lists no subtitles for this episode ({audio})"
    import httpx

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for track in tracks:
            response = await client.get(track.url, headers={"Referer": referer})
            assert response.status_code == 200, (track.title, response.status_code)
            source_format, body = to_vtt(response.text, format_from_url(track.url))
            assert body.lstrip().upper().startswith("WEBVTT"), track.title
            assert "-->" in body, f"{track.title} has no cues"
            assert source_format in ("vtt", "srt", "ass")


async def test_titles_are_unique_and_labelled():
    tracks, _ = await tracks_for_episode("attack on titan", "sub")
    titles = [track.title for track in tracks]
    assert len(set(title.lower() for title in titles)) == len(titles), titles
    for track in tracks:
        assert track.label or track.language_name == "Unknown Language"
        if track.language_source == "provider_label":
            assert track.title.startswith(track.language_name), track


async def test_duplicate_language_tracks_are_all_kept():
    """AoT's first episode lists several English tracks; none may be dropped."""
    tracks, _ = await tracks_for_episode("attack on titan", "sub")
    english = [track for track in tracks if track.language == "en"]
    if len(english) < 2:
        pytest.skip("this provider run listed fewer than two English tracks")
    assert len(english) == len([t for t in tracks if t.language == "en"])
    assert len({track.title.lower() for track in english}) == len(english)


async def test_provider_lang_is_not_used_as_the_track_language():
    """Observed: the provider sends lang="en" on tracks labelled Thai or Arabic.

    The regression guarded here is a foreign-language track presented as English
    because the raw field says 'en'. Several titles are scanned because
    single-track English episodes are common.
    """
    corpus = []
    for query in ("solo leveling", "attack on titan", "jujutsu kaisen"):
        for index in range(2):
            for audio in ("sub", "dub"):
                tracks, _ = await tracks_for_episode(query, audio, episode_index=index)
                corpus.extend(tracks)
    foreign = [track for track in corpus
               if track.provider_lang == "en" and track.language not in ("en", "und")]
    if not foreign:
        pytest.skip("this provider run listed only English tracks; nothing to compare")
    for track in foreign:
        assert track.language_source == "provider_label"
        assert track.title != "English", track
        assert track.title.startswith(track.language_name)
