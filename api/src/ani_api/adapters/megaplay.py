"""MegaPlay (s-2) source extractor: AES-256-CBC over the `enc` payload.

Serves both AniKoto (`/ajax/server?get=`) and HiAnime's HD-1 server, which
return `https://megaplay.buzz/stream/s-2/<fileId>/<audio>?s=tcdn`.

The player decrypts `enc` in e1-player.min.js:

    SOURCE_ENC_KEY = "i?LMTAx0Q6,:}50U"   # zero-padded to 32 bytes
    SOURCE_ENC_IV  = "W0;27ToaUpl_P%'c"   # 16 bytes as-is
    padKeyBytes(value, size) -> size-byte array, value's UTF-8 prefix, zero fill
    AES-CBC decrypt(b64url(enc)) -> JSON {"file": "<master.m3u8>"}

No third-party dependency: `cryptography` is not in requirements.txt and AES is
available through the platform `openssl` CLI, so nothing new is installed.
The key is upstream's public player constant, not a user secret.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import os
from typing import Any

ENC_KEY = "i?LMTAx0Q6,:}50U"
ENC_IV = "W0;27ToaUpl_P%'c"
GET_SOURCES = "https://megaplay.buzz/stream/getSources?id={file_id}"
PLAYER_ORIGIN = "https://megaplay.buzz/"


class MegaPlayError(RuntimeError):
    """Raised when a MegaPlay payload cannot be decrypted or validated."""


def _pad(value: str, size: int) -> bytes:
    return value.encode("utf-8")[:size].ljust(size, b"\0")


def _b64url(raw: str) -> bytes:
    text = str(raw).replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    return base64.b64decode(text)


def decrypt_enc(enc: str) -> dict[str, Any]:
    """Decrypt a MegaPlay `enc` payload into its JSON body."""
    binary = shutil.which("openssl")
    if not binary:
        raise MegaPlayError("openssl is unavailable; cannot decrypt enc payload")
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        handle.write(_b64url(enc))
        path = handle.name
    try:
        done = subprocess.run(
            [binary, "enc", "-d", "-aes-256-cbc",
             "-K", _pad(ENC_KEY, 32).hex(), "-iv", _pad(ENC_IV, 16).hex(), "-in", path],
            capture_output=True, timeout=10,
        )
    finally:
        os.unlink(path)
    if done.returncode != 0:
        raise MegaPlayError("enc decrypt failed")
    try:
        body = json.loads(done.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MegaPlayError("enc payload is not JSON") from exc
    if not isinstance(body, dict) or not isinstance(body.get("file"), str):
        raise MegaPlayError("enc payload has no file")
    return body


def master_url(payload: dict[str, Any]) -> str:
    """`{"file": "<m3u8>"}` -> the master playlist URL, or MegaPlayError."""
    url = payload.get("file") if isinstance(payload, dict) else None
    if not isinstance(url, str) or not url.startswith("https://"):
        raise MegaPlayError("decrypted payload carries no https master playlist")
    return url


def file_id_from_embed(embed_url: str) -> str:
    parts = [p for p in embed_url.split("?")[0].split("/") if p]
    for index, part in enumerate(parts):
        if part == "stream" and index + 2 < len(parts):
            return parts[index + 2]
    raise MegaPlayError("embed url has no file id")
