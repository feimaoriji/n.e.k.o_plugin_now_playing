"""Best-effort current-lyric lookup backed by the anonymous NetEase API.

Standalone module: only the Python standard library is used, so it does not
require the NEKO/plugin SDK nor ``pyncm_async`` (the NetEase Music plugin is
free to reuse it, but this file works on its own).

Public API
----------
``get_current_lyric(title, artist, position_ms) -> str``
    The lyric line active at ``position_ms`` for the given track, or ``""``.
    Never raises: no network, no match, no lyrics or a timeout all collapse to
    ``""``.

``fetch_lyrics(title, artist) -> list[tuple[int, str]] | None``
    Full parsed ``(time_ms, text)`` list for a track (cached), or ``None``.

``clear_cache()``
    Drop the in-memory search / lyric caches.

CLI
---
``python lyrics.py "Overthinker" "INZO" 12000``
    Print the lyric active at 12.0s.
``python lyrics.py "Overthinker" "INZO" --dump``
    Print every parsed lyric line with its timestamp.

How it works
------------
1. ``POST https://music.163.com/api/search/get/web`` with ``s="<title> <artist>"``
   to resolve a ``song_id`` (exact title + artist match preferred).
2. ``GET  https://music.163.com/api/song/lyric?id=<id>&lv=1&kv=1&tv=-1`` to get
   the LRC text.
3. Parse ``[mm:ss.xx]`` timestamps and select the line active at a position.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

__all__ = ["get_current_lyric", "fetch_lyrics", "parse_lrc", "clear_cache"]

SEARCH_URL = "https://music.163.com/api/search/get/web"
LYRIC_URL = "https://music.163.com/api/song/lyric"
REQUEST_TIMEOUT = 6.0
MAX_CANDIDATES = 10

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "N.E.K.O now_playing/0.1"
)
_BASE_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Referer": "https://music.163.com/",
    "Cookie": "appver=8.9.70; os=pc",
}

# [mm:ss.xx] / [mm:ss] / [mm:ss.xxx] / [mm:ss:xx]
_TIME_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_TAG_RE = re.compile(r"^\[(ti|ar|al|by|offset|re|ve|length|kana|hash):", re.I)
_OFFSET_RE = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.I)

# Caches persist for the life of the process: searching the same track twice
# (very common when polling) reuses the resolved song id / parsed lyrics.
_SEARCH_CACHE: dict[tuple[str, str], int | None] = {}
_LYRIC_CACHE: dict[tuple[str, str], list[tuple[int, str]] | None] = {}


# ---------------------------------------------------------------------------
# Normalisation / matching
# ---------------------------------------------------------------------------
def _normalize(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(text.split())


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\r", "").split())


def _artist_names(song: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in song.get("artists") or song.get("ar") or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return names


def _select_candidate(
    title: str, artist: str, songs: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Pick the best matching song; refuses fuzzy/wrong matches when possible."""

    target_title = _normalize(title)
    target_artist = _normalize(artist)
    if not target_title:
        return None

    exact_title: list[dict[str, Any]] = []
    loose: list[dict[str, Any]] = []
    for song in songs:
        song_title = _normalize(song.get("name"))
        if not song_title:
            continue
        artists = [_normalize(a) for a in _artist_names(song)]
        artist_ok = (
            not target_artist
            or target_artist in artists
            or any(target_artist in a or a in target_artist for a in artists if a)
        )
        if song_title == target_title:
            if artist_ok or not target_artist:
                return song
            exact_title.append(song)
        elif target_artist and artist_ok and (
            target_title in song_title or song_title in target_title
        ):
            loose.append(song)

    if exact_title:
        return exact_title[0]
    # Only accept a loose match when it is unambiguous.
    if len(loose) == 1:
        return loose[0]
    return None


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------
def _http_post(url: str, data: dict[str, str]) -> bytes | None:
    body = urllib.parse.urlencode(data).encode("utf-8")
    headers = dict(_BASE_HEADERS)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _http_get(url: str) -> bytes | None:
    request = urllib.request.Request(url, headers=dict(_BASE_HEADERS))
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _loads(raw: bytes | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# NetEase lookups
# ---------------------------------------------------------------------------
def resolve_song_id(title: str, artist: str) -> int | None:
    """Resolve a NetEase ``song_id`` for ``(title, artist)``; cached, never raises."""

    key = (_normalize(title), _normalize(artist))
    if not key[0]:
        return None
    if key in _SEARCH_CACHE:
        return _SEARCH_CACHE[key]

    query = " ".join(part for part in (title.strip(), artist.strip()) if part)
    payload = _loads(
        _http_post(
            SEARCH_URL,
            {"s": query, "type": "1", "offset": "0", "limit": str(MAX_CANDIDATES)},
        )
    )
    song_id: int | None = None
    if payload and payload.get("code") == 200:
        result = payload.get("result")
        songs = result.get("songs") if isinstance(result, dict) else None
        if isinstance(songs, list):
            song = _select_candidate(title, artist, songs)
            if song is not None:
                raw_id = song.get("id")
                if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
                    song_id = raw_id
                elif isinstance(raw_id, str) and raw_id.isdigit() and int(raw_id) > 0:
                    song_id = int(raw_id)

    # Cache successful and negative lookups alike (avoid hammering the API);
    # negative results are cached only for the process lifetime.
    _SEARCH_CACHE[key] = song_id
    return song_id


def _fetch_lrc(song_id: int) -> str | None:
    url = f"{LYRIC_URL}?id={song_id}&lv=1&kv=1&tv=-1"
    payload = _loads(_http_get(url))
    if not payload or payload.get("code") != 200:
        return None
    lrc = payload.get("lrc")
    text = lrc.get("lyric") if isinstance(lrc, dict) else None
    if not isinstance(text, str) or not text.strip():
        # Fall back to the translation track when the original is instrumental.
        tlyric = payload.get("tlyric")
        text = tlyric.get("lyric") if isinstance(tlyric, dict) else None
    return text if isinstance(text, str) and text.strip() else None


def parse_lrc(text: str) -> list[tuple[int, str]]:
    """Parse an LRC string into a sorted list of ``(time_ms, text)`` tuples."""

    if not text:
        return []

    raw_offset = _OFFSET_RE.search(text)
    offset_ms = int(raw_offset.group(1)) if raw_offset else 0

    entries: list[tuple[int, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _META_TAG_RE.match(line):
            continue
        stamps = list(_TIME_TAG_RE.finditer(line))
        if not stamps:
            continue
        text_part = _TIME_TAG_RE.sub("", line).strip()
        for match in stamps:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            fraction = match.group(3) or ""
            millis = 0
            if fraction:
                if len(fraction) == 1:
                    millis = int(fraction) * 100
                elif len(fraction) == 2:
                    millis = int(fraction) * 10
                else:
                    millis = int(fraction[:3])
            time_ms = minutes * 60_000 + seconds * 1000 + millis + offset_ms
            if time_ms < 0:
                time_ms = 0
            entries.append((time_ms, text_part))

    entries.sort(key=lambda item: item[0])
    return entries


def fetch_lyrics(title: str, artist: str = "") -> list[tuple[int, str]] | None:
    """Fetch + parse the LRC for a track. Cached; returns None when unavailable."""

    key = (_normalize(title), _normalize(artist))
    if not key[0]:
        return None
    if key in _LYRIC_CACHE:
        return _LYRIC_CACHE[key]

    lyrics: list[tuple[int, str]] | None = None
    song_id = resolve_song_id(title, artist)
    if song_id is not None:
        lrc = _fetch_lrc(song_id)
        if lrc:
            parsed = parse_lrc(lrc)
            if parsed:
                lyrics = parsed

    _LYRIC_CACHE[key] = lyrics
    return lyrics


def line_at(lyrics: list[tuple[int, str]] | None, position_ms: int | float | None) -> str:
    """Return the lyric line active at ``position_ms`` (last start <= position)."""

    if not lyrics:
        return ""
    try:
        position = int(position_ms) if position_ms is not None else 0
    except (TypeError, ValueError):
        position = 0
    if position < 0:
        position = 0

    current = ""
    for time_ms, text in lyrics:
        if time_ms > position:
            break
        if text:
            current = text
    return current


def get_current_lyric(
    title: str, artist: str = "", position_ms: int | float | None = 0
) -> str:
    """Best-effort current lyric line. Returns ``""`` on any failure."""

    try:
        if not title or not str(title).strip():
            return ""
        lyrics = fetch_lyrics(str(title), str(artist or ""))
        return line_at(lyrics, position_ms)
    except Exception:
        return ""


def clear_cache() -> None:
    _SEARCH_CACHE.clear()
    _LYRIC_CACHE.clear()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    args = [a for a in argv[1:]]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    dump = "--dump" in args
    args = [a for a in args if a != "--dump"]
    if not args:
        print(__doc__)
        return 0

    title = args[0]
    artist = args[1] if len(args) >= 2 else ""
    position_ms: int | float = 0
    if len(args) >= 3 and not dump:
        try:
            position_ms = float(args[2])
        except ValueError:
            position_ms = 0.0

    lyrics = fetch_lyrics(title, artist)
    if lyrics is None:
        print(json.dumps({"title": title, "artist": artist, "lyrics": None}, ensure_ascii=False))
        return 1

    if dump:
        for time_ms, text in lyrics:
            print(f"[{time_ms:>8}ms] {text}")
        return 0

    print(
        json.dumps(
            {
                "title": title,
                "artist": artist,
                "position_ms": position_ms,
                "line": line_at(lyrics, position_ms),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
