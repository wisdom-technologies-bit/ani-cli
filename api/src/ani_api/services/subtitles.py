"""Subtitle tracks: labelling, format detection and format conversion.

Everything in this module is derived from what the provider actually sends. Two
observations from live traffic drive the design:

1. The provider's ``lang`` field is a constant ``"en"`` on every track observed,
   including tracks labelled Arabic, Thai, Portuguese and Japanese. It therefore
   cannot be used as the track language. The label is the only trustworthy
   language metadata the provider supplies, so labels are parsed (never invented)
   and ``lang`` is preserved as a raw provider field.
2. A single episode can carry one track or ten. Nothing here keeps only the first
   one, and nothing assumes a track is English.

Speech recognition, audio transcription and audio-language detection are
explicitly out of scope: this module never looks at the audio at all.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional
from urllib.parse import urlsplit

# Provider label language word -> (BCP-47-ish code, canonical English name).
# Only labels the provider actually words as a language are mapped; anything
# else stays unknown rather than being guessed.
LANGUAGES: dict[str, tuple[str, str]] = {
    "english": ("en", "English"),
    "japanese": ("ja", "Japanese"),
    "spanish": ("es", "Spanish"),
    "espanol": ("es", "Spanish"),
    "español": ("es", "Spanish"),
    "latino": ("es", "Spanish"),
    "portuguese": ("pt", "Portuguese"),
    "french": ("fr", "French"),
    "francais": ("fr", "French"),
    "german": ("de", "German"),
    "deutsch": ("de", "German"),
    "italian": ("it", "Italian"),
    "russian": ("ru", "Russian"),
    "arabic": ("ar", "Arabic"),
    "thai": ("th", "Thai"),
    "indonesian": ("id", "Indonesian"),
    "chinese": ("zh", "Chinese"),
    "mandarin": ("zh", "Chinese"),
    "cantonese": ("zh", "Chinese"),
    "korean": ("ko", "Korean"),
    "vietnamese": ("vi", "Vietnamese"),
    "malay": ("ms", "Malay"),
    "turkish": ("tr", "Turkish"),
    "polish": ("pl", "Polish"),
    "dutch": ("nl", "Dutch"),
    "hebrew": ("he", "Hebrew"),
    "greek": ("el", "Greek"),
    "bulgarian": ("bg", "Bulgarian"),
    "danish": ("da", "Danish"),
    "finnish": ("fi", "Finnish"),
    "norwegian": ("no", "Norwegian"),
    "swedish": ("sv", "Swedish"),
    "czech": ("cs", "Czech"),
    "hungarian": ("hu", "Hungarian"),
    "romanian": ("ro", "Romanian"),
    "ukrainian": ("uk", "Ukrainian"),
    "hindi": ("hi", "Hindi"),
    "bengali": ("bn", "Bengali"),
    "tamil": ("ta", "Tamil"),
    "telugu": ("te", "Telugu"),
    "filipino": ("fil", "Filipino"),
    "tagalog": ("tl", "Tagalog"),
    "persian": ("fa", "Persian"),
    "farsi": ("fa", "Persian"),
    "urdu": ("ur", "Urdu"),
    "croatian": ("hr", "Croatian"),
    "serbian": ("sr", "Serbian"),
    "bosnian": ("bs", "Bosnian"),
    "slovak": ("sk", "Slovak"),
    "slovenian": ("sl", "Slovenian"),
    "estonian": ("et", "Estonian"),
    "latvian": ("lv", "Latvian"),
    "lithuanian": ("lt", "Lithuanian"),
    "catalan": ("ca", "Catalan"),
    "basque": ("eu", "Basque"),
    "galician": ("gl", "Galician"),
    "albanian": ("sq", "Albanian"),
    "macedonian": ("mk", "Macedonian"),
    "icelandic": ("is", "Icelandic"),
    "irish": ("ga", "Irish"),
    "welsh": ("cy", "Welsh"),
    "swahili": ("sw", "Swahili"),
    "afrikaans": ("af", "Afrikaans"),
    "burmese": ("my", "Burmese"),
    "khmer": ("km", "Khmer"),
    "lao": ("lo", "Lao"),
    "mongolian": ("mn", "Mongolian"),
    "nepali": ("ne", "Nepali"),
    "sinhala": ("si", "Sinhala"),
    "kazakh": ("kk", "Kazakh"),
    "uzbek": ("uz", "Uzbek"),
    "azerbaijani": ("az", "Azerbaijani"),
    "georgian": ("ka", "Georgian"),
    "armenian": ("hy", "Armenian"),
    "maltese": ("mt", "Maltese"),
}

CODE_TO_NAME = {code: name for code, name in LANGUAGES.values()}
UNKNOWN_LANGUAGE = "Unknown Language"
UNKNOWN_CODE = "und"

# Formats the service can hand to a player, directly or after conversion.
SUPPORTED_FORMATS = ("vtt", "srt", "ass")
EXTENSION_FORMATS = {
    "vtt": "vtt", "webvtt": "vtt",
    "srt": "srt", "subrip": "srt",
    "ass": "ass", "ssa": "ass",
}
# Extensions that are demonstrably not a subtitle (thumbnail sprites, images).
NON_SUBTITLE_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "webp", "json", "m3u8", "mp4", "ts", "html", "js",
}

_TOKEN_SPLIT = re.compile(r"[()\[\]{}]|\s+[-–—:|]\s+|\s*[-–—:|]\s*|\s+")
_SIGNS = re.compile(r"\b(signs?|songs?|karaoke|signs\s*&\s*songs)\b", re.I)
_FORCED = re.compile(r"\bforced\b", re.I)
_DUBTITLE = re.compile(r"\bdub[\s-]?title[s]?\b", re.I)
_FULL = re.compile(r"\bfull\b", re.I)


def _clean_token(token: str) -> str:
    token = unicodedata.normalize("NFC", token).strip()
    return token.strip("-_–—:|,.;\"' ")


def split_label_parts(label: str) -> tuple[list[str], str]:
    """Split a provider label into (cleaned tokens, lowered leading word)."""
    tokens = [t for t in (_clean_token(p) for p in _TOKEN_SPLIT.split(label or "")) if t]
    return tokens, (tokens[0].lower() if tokens else "")


def parse_label(label: str) -> tuple[Optional[str], str, Optional[str]]:
    """Provider label -> (language code, language name, descriptor).

    Only what the provider wrote is used. A label that does not name a known
    language yields ``(None, "Unknown Language", <label-derived descriptor>)``.
    """
    tokens, head = split_label_parts(label or "")
    if not tokens:
        return None, UNKNOWN_LANGUAGE, None

    language = LANGUAGES.get(head)
    remainder = tokens[1:] if language else []
    if not language:
        # No leading language word: a language named later in the label (for
        # example inside brackets) still identifies the track. Otherwise the
        # whole label is the descriptor and the language stays unknown.
        for token in tokens:
            if token.lower() in LANGUAGES:
                language = LANGUAGES[token.lower()]
                break
        if language is None:
            return None, UNKNOWN_LANGUAGE, " ".join(tokens)
        descriptor = " ".join(t for t in tokens if t.lower() not in LANGUAGES)
        return language[0], language[1], (descriptor.strip(" -,;|") or None)

    descriptor_tokens: list[str] = []
    for token in remainder:
        stripped = _clean_token(token)
        if not stripped:
            continue
        # "Chinese - Chinese (Simplified)" repeats the language; drop the repeat.
        if stripped.lower() in LANGUAGES and not descriptor_tokens:
            continue
        descriptor_tokens.append(stripped)
    descriptor = " ".join(descriptor_tokens).strip()
    descriptor = re.sub(r"\s+", " ", descriptor).strip(" -,;|")
    return language[0], language[1], (descriptor or None)


def track_kind(label: str) -> str:
    """Provider-declared track kind, only when the label says enough."""
    text = label or ""
    if _DUBTITLE.search(text):
        return "dubtitle"
    if _SIGNS.search(text):
        return "signs"
    if _FORCED.search(text):
        return "forced"
    if _FULL.search(text):
        return "full"
    return "full"


def display_title(language_name: str, descriptor: Optional[str], label: str) -> str:
    if language_name != UNKNOWN_LANGUAGE:
        return f"{language_name} ({descriptor})" if descriptor else language_name
    if descriptor:
        return descriptor
    if (label or "").strip():
        return label.strip()
    return UNKNOWN_LANGUAGE


def format_from_url(url: str) -> Optional[str]:
    """Format implied by the URL extension, or None when the URL says nothing."""
    path = urlsplit(url or "").path
    filename = path.rsplit("/", 1)[-1]
    if "." not in filename:
        return None
    extension = filename.rsplit(".", 1)[-1].lower()
    return EXTENSION_FORMATS.get(extension)


def non_subtitle_url(url: str) -> bool:
    path = urlsplit(url or "").path
    filename = path.rsplit("/", 1)[-1]
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return extension in NON_SUBTITLE_EXTENSIONS


def sniff_format(text: str) -> str:
    """Format of subtitle text, detected from its content."""
    body = (text or "").lstrip("\ufeff").lstrip()
    head = body[:4096]
    if head.upper().startswith("WEBVTT"):
        return "vtt"
    if "[script info]" in head.lower() or "\ndialogue:" in ("\n" + head.lower()):
        return "ass"
    if re.search(r"\d{1,2}:\d{2}:\d{2},\d{1,3}\s*-->", head):
        return "srt"
    if re.search(r"\d{1,2}:\d{2}:\d{2}\.\d{1,3}\s*-->", head):
        return "vtt"
    return "unknown"


class SubtitleConversionError(ValueError):
    """The bytes are not a subtitle format this service can serve."""


def _srt_to_vtt(text: str) -> str:
    body = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"^\s*\d+\s*$", "", body, flags=re.M)          # cue numbers
    body = re.sub(r"(\d{1,2}:\d{2}:\d{2}),(\d{1,3})", r"\1.\2", body)  # commas -> dots
    body = re.sub(r"\n{3,}", "\n\n", body).strip("\n")
    return f"WEBVTT\n\n{body}\n"


def _ass_time(value: str) -> Optional[str]:
    match = re.match(r"\s*(\d+):(\d{1,2}):(\d{1,2})[.,](\d{1,3})\s*$", value or "")
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    millis = int(fraction.ljust(3, "0")[:3])
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}.{millis:03d}"


def _ass_to_vtt(text: str) -> str:
    body = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = body.split("\n")
    index: dict[str, int] = {}
    cues: list[str] = []
    in_events = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_events = stripped.lower().startswith("[events]")
            continue
        if not in_events:
            continue
        if stripped.lower().startswith("format:"):
            index = {name.strip().lower(): position
                     for position, name in enumerate(stripped.split(":", 1)[1].split(","))}
            continue
        if not stripped.lower().startswith("dialogue:"):
            continue                                   # Comment:, Picture:, Sound:
        if not index:
            index = {"layer": 0, "start": 1, "end": 2, "style": 3, "name": 4,
                     "marginl": 5, "marginr": 6, "marginv": 7, "effect": 8, "text": 9}
        fields = stripped.split(":", 1)[1].split(",", max(len(index) - 1, 9))
        if len(fields) <= max(index.get("start", 1), index.get("end", 2), index.get("text", 9)):
            continue
        start = _ass_time(fields[index.get("start", 1)])
        end = _ass_time(fields[index.get("end", 2)])
        if not start or not end:
            continue
        payload = fields[index.get("text", 9)]
        payload = re.sub(r"\{[^{}]*\}", "", payload)   # override tags
        payload = payload.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
        payload = payload.strip()
        # Drawing commands ("m 0 0 l ...") are vector shapes, not text.
        if not payload or re.match(r"^[mnlbspc]\s+\d", payload):
            continue
        cues.append(f"{start} --> {end}\n{payload}")
    if not cues:
        raise SubtitleConversionError("ASS/SSA file has no dialogue cues")
    return "WEBVTT\n\n" + "\n\n".join(cues) + "\n"


def to_vtt(text: str, source_format: Optional[str] = None) -> tuple[str, str]:
    """Return (source_format, webvtt text). Raises when nothing can be served."""
    if not (text or "").strip():
        raise SubtitleConversionError("subtitle track is empty")
    detected = sniff_format(text)
    format_ = detected if detected != "unknown" else (source_format or "unknown")
    if format_ == "vtt":
        body = text.lstrip("\ufeff").replace("\r\n", "\n")
        if not body.lstrip().upper().startswith("WEBVTT"):
            body = "WEBVTT\n\n" + body.lstrip()
        return "vtt", body
    if format_ == "srt":
        return "srt", _srt_to_vtt(text)
    if format_ == "ass":
        return "ass", _ass_to_vtt(text)
    raise SubtitleConversionError("unsupported subtitle format")


def build_track_title(language_name: str, descriptor: Optional[str], label: str,
                      used: set[str]) -> str:
    """Title unique within the episode, so two same-language tracks stay distinct."""
    base = display_title(language_name, descriptor, label)
    title = base
    counter = 2
    while title.lower() in used:
        title = f"{base} ({counter})"
        counter += 1
    used.add(title.lower())
    return title


def dedupe_tracks(tracks: Iterable) -> list:
    """Drop exact duplicates (same URL) while keeping provider order."""
    seen: set[str] = set()
    out = []
    for track in tracks:
        key = track.url
        if key in seen:
            continue
        seen.add(key)
        out.append(track)
    return out


def _track_id(position: int) -> str:
    return f"sub-{position + 1}"


def enrich_tracks(tracks: Iterable) -> list:
    """Attach ids, language, display titles, format and playability to raw tracks.

    Input tracks carry only what the provider sent (``url``, ``label``,
    ``provider_lang``, ``default``). Order is the provider's order, which is what a
    player shows, and duplicate URLs are collapsed.
    """
    unique = dedupe_tracks(tracks)
    used_titles: set[str] = set()
    for position, track in enumerate(unique):
        code, name, descriptor = parse_label(track.label or "")
        track.index = position
        track.id = _track_id(position)
        track.language = code or UNKNOWN_CODE
        track.language_name = name
        track.language_source = "provider_label" if code else "unknown"
        track.title = build_track_title(name, descriptor, track.label or "", used_titles)
        track.kind = track_kind(track.label or "")
        track.language_inferred = track.language if code else None
        track.language_verified = False          # audio is never analysed

        declared = format_from_url(track.url)
        if declared in SUPPORTED_FORMATS:
            track.format = declared
            track.format_source = "url_extension"
            track.playable = True
            track.converted = declared != "vtt"
        else:
            # Unknown until sniffed: never advertise an unproven format as playable.
            track.format = "unknown"
            track.format_source = "unknown"
            track.playable = False
            track.converted = False
    return unique


async def detect_unknown_formats(tracks: Iterable, fetch, limit: int = 3) -> None:
    """Sniff tracks whose URL states no format. Best effort, bounded, never fatal."""
    candidates = [track for track in tracks if track.format == "unknown"][:limit]
    for track in candidates:
        text = await fetch(track.url)
        if not text:
            continue
        detected = sniff_format(text)
        if detected in SUPPORTED_FORMATS:
            track.format = detected
            track.format_source = "sniffed"
            track.playable = True
            track.converted = detected != "vtt"
