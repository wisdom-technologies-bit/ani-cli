"""Content identity: a package that names another anime must be refused."""
from ani_api.services import content_identity as ci

# The real One Piece ep 1 package an AniKoto slug pointed at: its shipped caption
# track is Toilet-bound Hanako-kun 2, repeated as a title card.
FOREIGN_VTT = """WEBVTT

00:15.990 --> 00:22.000
<b>7</b>

01:36.450 --> 02:05.220
<b>Toilet-bound Hanako-kun 2</b>

02:19.860 --> 02:26.740
<b>Toilet-bound Hanako-kun 2</b>

05:00.000 --> 05:04.000
<b>Part VII: Make Sense</b>

06:00.000 --> 06:06.000
<b>Toilet-bound Hanako-kun 2</b>

07:00.000 --> 07:03.000
<b>Toilet-bound Hanako-kun 2</b>

08:00.000 --> 08:04.000
<b>Mokke Hutch</b>

09:00.000 --> 09:04.000
<b>Toilet-bound Hanako-kun 2</b>
"""

# A normal dialogue track: no title card, only speech.
DIALOGUE_VTT = """WEBVTT

00:01.000 --> 00:03.000
What?

00:04.000 --> 00:06.000
He is gone!

00:07.000 --> 00:09.000
What?

00:10.000 --> 00:12.000
Run now!
"""


def test_title_cards_isolates_a_repeated_title():
    assert ci.title_cards(FOREIGN_VTT) == ["Toilet-bound Hanako-kun 2"]


def test_title_cards_ignores_ordinary_dialogue():
    assert ci.title_cards(DIALOGUE_VTT) == []


def test_same_show_tolerates_season_markers():
    assert ci._same_show("Toilet-bound Hanako-kun 2",
                         ["Toilet-bound Hanako-kun Season 2"])


def test_same_show_rejects_a_different_franchise():
    assert not ci._same_show("Toilet-bound Hanako-kun 2",
                             ["Attack on Titan", "Shingeki no Kyojin"])


def test_verify_flags_a_foreign_package(monkeypatch):
    monkeypatch.setattr(ci, "_anilist_search", lambda term, timeout=8.0: {
        "id": 170892, "idMal": 53924,
        "title": {"english": "Toilet-bound Hanako-kun Season 2",
                  "romaji": "Jibaku Shounen Hanako-kun 2", "native": "地縛少年花子くん２"},
        "synonyms": [], "format": "TV"})
    assert ci.verify([FOREIGN_VTT], 21, ["One Piece"]) == "foreign"


def test_verify_accepts_the_requested_show(monkeypatch):
    monkeypatch.setattr(ci, "_anilist_search", lambda term, timeout=8.0: {
        "id": 21, "idMal": 21,
        "title": {"english": "One Piece", "romaji": "ONE PIECE", "native": "ワンピース"},
        "synonyms": [], "format": "TV"})
    card = "WEBVTT\n\n00:01.000 --> 00:02.000\n<b>One Piece</b>\n\n" * 4
    assert ci.verify([card], 21, ["One Piece"]) == "match"


def test_verify_stays_silent_without_evidence(monkeypatch):
    monkeypatch.setattr(ci, "_anilist_search", lambda term, timeout=8.0: None)
    assert ci.verify([DIALOGUE_VTT], 21, ["One Piece"]) == "unknown"
    assert ci.verify([], 21, ["One Piece"]) == "unknown"


def test_verify_stream_caches_the_verdict(monkeypatch):
    calls = []

    def fake_search(term, timeout=8.0):
        calls.append(term)
        return {"id": 170892, "idMal": 53924,
                "title": {"english": "Toilet-bound Hanako-kun Season 2"},
                "synonyms": [], "format": "TV"}

    monkeypatch.setattr(ci, "_anilist_search", fake_search)
    ci._VERDICT_CACHE.clear()
    stream = {"file_id": "2142", "subtitles": [{"url": "u1", "label": "English"}]}
    first = ci.verify_stream(stream, 21, ["One Piece"], lambda url: FOREIGN_VTT)
    second = ci.verify_stream(stream, 21, ["One Piece"], lambda url: FOREIGN_VTT)
    assert first == second == "foreign"
    assert len(calls) == 1
