"""Read the current Windows media session (SMTC / GSMTC) via WinRT.

This module is intentionally standalone: it only depends on the WinRT
projection packages (or the legacy ``winsdk`` package) plus the Python
standard library. It does **not** import the NEKO/plugin SDK, so it can be
reused, unit-tested and run as a plain script.

Public API
----------
``get_now_playing() -> dict | None``
    Snapshot of the current media session. Never raises: any missing
    session / metadata / permission problem is reported inside the returned
    dict via the ``error`` (and optional ``error_detail`` / ``warnings``)
    fields, with ``playing`` set to ``False``.

``aget_now_playing()``
    Async variant with the same contract, for callers that already run an
    event loop and prefer not to block.

CLI
---
``python smtc.py``             print one JSON snapshot and exit.
``python smtc.py --watch 2``   print a JSON line every time the now-playing
                               state changes, polling every 2 seconds.

Returned dict keys
------------------
``playing`` (bool), ``title``, ``artist``, ``album``, ``status``,
``position_ms`` (int | None), ``duration_ms`` (int | None),
``source_app_id``, ``fetched_at`` (ISO-8601 string).
Failure snapshots additionally carry ``error`` / ``error_detail`` / ``warnings``.

Read-only: this module never controls the player.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = ["get_now_playing", "aget_now_playing", "winrt_backend", "winrt_error"]

# Windows.Foundation.TimeSpan is expressed in 100-nanosecond ticks in the
# legacy projections; some projections already convert it to datetime.timedelta.
_TICKS_PER_MS = 10_000

_STATUS_BY_VALUE = {
    0: "closed",
    1: "opened",
    2: "changing",
    3: "stopped",
    4: "playing",
    5: "paused",
}

# ---------------------------------------------------------------------------
# WinRT import (modular packages first, legacy winsdk as a fallback).
# Import failures are captured, never raised, so importing this module is
# always safe even on machines without WinRT installed.
# ---------------------------------------------------------------------------
_Manager: Any = None
_PlaybackStatus: Any = None
winrt_backend: str | None = None
winrt_error: str | None = None

try:  # Preferred: modular winrt-* packages (Python 3.11).
    from winrt.windows.media.control import (  # type: ignore
        GlobalSystemMediaTransportControlsSessionManager as _Manager,
    )
    from winrt.windows.media.control import (  # type: ignore
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _PlaybackStatus,
    )

    winrt_backend = "winrt-runtime (modular winrt-* packages)"
except Exception as _exc:  # pragma: no cover - depends on host environment
    _first_error = _exc
    try:  # Fallback: legacy monolithic winsdk package.
        from winsdk.windows.media.control import (  # type: ignore
            GlobalSystemMediaTransportControlsSessionManager as _Manager,
        )
        from winsdk.windows.media.control import (  # type: ignore
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as _PlaybackStatus,
        )

        winrt_backend = "winsdk (legacy monolithic package)"
    except Exception as _exc2:  # pragma: no cover - depends on host environment
        _Manager = None
        _PlaybackStatus = None
        winrt_backend = None
        winrt_error = f"winrt: {_first_error!r}; winsdk: {_exc2!r}"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    """Local-time ISO-8601 timestamp with millisecond precision."""

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _empty_payload(fetched_at: str | None = None) -> dict[str, Any]:
    return {
        "playing": False,
        "title": "",
        "artist": "",
        "album": "",
        "status": None,
        "position_ms": None,
        "duration_ms": None,
        "source_app_id": None,
        "fetched_at": fetched_at or _now_iso(),
    }


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return ""
    return text.strip()


def _safe(obj: Any, *names: str, default: Any = None) -> Any:
    """Return the first present, non-None attribute; swallow projection errors."""

    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if value is not None:
            return value
    return default


def _to_ms(value: Any) -> int | None:
    """Convert a WinRT time value to integer milliseconds, or None."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, timedelta):
        return int(round(value.total_seconds() * 1000.0))
    if isinstance(value, (int, float)):
        # Legacy projections expose Windows.Foundation.TimeSpan as 100ns ticks.
        return int(round(float(value) / _TICKS_PER_MS))
    # Some projections expose a ``duration`` attribute or ``total_seconds``.
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        try:
            return int(round(float(total_seconds()) * 1000.0))
        except Exception:
            pass
    inner = getattr(value, "duration", None)
    if inner is not None and inner is not value:
        return _to_ms(inner)
    return None


def _status_name(status: Any) -> str | None:
    if status is None:
        return None
    name = getattr(status, "name", None)
    if isinstance(name, str) and name:
        return name.lower()
    try:
        return _STATUS_BY_VALUE.get(int(status))
    except Exception:
        return str(status)


def _playback_status_of(session: Any) -> Any:
    try:
        return _safe(session.get_playback_info(), "playback_status")
    except Exception:
        return None


def _pick_session(manager: Any) -> Any:
    """Prefer the current session; fall back to a playing / any known session."""

    try:
        session = manager.get_current_session()
    except Exception:
        session = None
    if session is not None:
        return session

    try:
        sessions = list(manager.get_sessions())
    except Exception:
        sessions = []

    fallback = None
    for candidate in sessions:
        if _status_name(_playback_status_of(candidate)) == "playing":
            return candidate
        if fallback is None:
            fallback = candidate
    return fallback


async def _request_manager() -> Any:
    """Await ``request_async()``; tolerate projections that return it directly."""

    operation = _Manager.request_async()
    if hasattr(operation, "__await__"):
        return await operation
    return operation


async def _read_session(session: Any, fetched_at: str) -> dict[str, Any]:
    payload = _empty_payload(fetched_at)
    warnings: list[str] = []

    payload["source_app_id"] = _clean(
        _safe(session, "source_app_user_model_id", default="")
    ) or None

    # --- playback status ---------------------------------------------------
    try:
        payload["status"] = _status_name(
            _safe(session.get_playback_info(), "playback_status")
        )
    except Exception as exc:  # pragma: no cover - host dependent
        warnings.append(f"playback_info: {type(exc).__name__}: {exc}")
    payload["playing"] = payload["status"] == "playing"

    # --- timeline (position / duration) -----------------------------------
    try:
        timeline = session.get_timeline_properties()
        position_ms = _to_ms(_safe(timeline, "position"))
        end_ms = _to_ms(_safe(timeline, "end_time", "duration"))
        start_ms = _to_ms(_safe(timeline, "start_time"))
        duration_ms: int | None = None
        if end_ms is not None:
            duration_ms = end_ms - (start_ms or 0)
            if duration_ms <= 0:
                duration_ms = end_ms if end_ms > 0 else None
        payload["position_ms"] = position_ms
        payload["duration_ms"] = duration_ms
    except Exception as exc:  # pragma: no cover - host dependent
        warnings.append(f"timeline: {type(exc).__name__}: {exc}")

    # --- metadata ----------------------------------------------------------
    try:
        properties = session.try_get_media_properties_async()
        if hasattr(properties, "__await__"):
            properties = await properties
        if properties is not None:
            payload["title"] = _clean(_safe(properties, "title", default=""))
            payload["artist"] = _clean(_safe(properties, "artist", default=""))
            payload["album"] = _clean(
                _safe(properties, "album_title", "album", default="")
            )
    except Exception as exc:  # pragma: no cover - host dependent
        warnings.append(f"media_properties: {type(exc).__name__}: {exc}")

    if not payload["title"] and payload.get("error") is None:
        payload["error"] = "no_metadata"

    if warnings:
        payload["warnings"] = warnings
    return payload


async def aget_now_playing() -> dict[str, Any] | None:
    """Async snapshot of the current media session. Never raises."""

    fetched_at = _now_iso()
    if _Manager is None:
        payload = _empty_payload(fetched_at)
        payload["error"] = "winrt_unavailable"
        payload["error_detail"] = winrt_error
        return payload

    try:
        manager = await _request_manager()
    except Exception as exc:
        payload = _empty_payload(fetched_at)
        payload["error"] = "manager_unavailable"
        payload["error_detail"] = f"{type(exc).__name__}: {exc}"
        return payload

    if manager is None:
        payload = _empty_payload(fetched_at)
        payload["error"] = "manager_unavailable"
        return payload

    session = _pick_session(manager)
    if session is None:
        payload = _empty_payload(fetched_at)
        payload["error"] = "no_active_session"
        return payload

    try:
        return await _read_session(session, fetched_at)
    except Exception as exc:  # pragma: no cover - defensive
        payload = _empty_payload(fetched_at)
        payload["error"] = "read_failed"
        payload["error_detail"] = f"{type(exc).__name__}: {exc}"
        return payload


def get_now_playing() -> dict[str, Any] | None:
    """Synchronous snapshot of the current media session. Never raises.

    Works both outside and inside a running asyncio event loop: when a loop
    is already running in the calling thread the coroutine is executed in a
    short-lived helper thread so we never touch the caller's loop.
    """

    coroutine = aget_now_playing()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(coroutine)
        except Exception as exc:  # pragma: no cover - defensive
            return _fatal_payload(exc)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coroutine).result()
    except Exception as exc:  # pragma: no cover - defensive
        return _fatal_payload(exc)


def _fatal_payload(exc: BaseException) -> dict[str, Any]:
    payload = _empty_payload()
    payload["error"] = "exception"
    payload["error_detail"] = f"{type(exc).__name__}: {exc}"
    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _fingerprint(payload: dict[str, Any] | None) -> tuple[Any, ...]:
    """Identity of the "now playing" state (ignores position/timestamps)."""

    if not isinstance(payload, dict):
        return (None,)
    return (
        payload.get("playing"),
        payload.get("title"),
        payload.get("artist"),
        payload.get("album"),
        payload.get("status"),
        payload.get("source_app_id"),
        payload.get("error"),
    )


def _print_once() -> int:
    print(json.dumps(get_now_playing(), ensure_ascii=False, indent=2))
    return 0


def _watch(interval: float) -> int:
    interval = max(0.2, interval)

    async def runner() -> None:
        last: tuple[Any, ...] | None = None
        while True:
            payload = await aget_now_playing()
            fingerprint = _fingerprint(payload)
            if fingerprint != last:
                print(json.dumps(payload, ensure_ascii=False))
                sys.stdout.flush()
                last = fingerprint
            await asyncio.sleep(interval)

    print(f"# watching now-playing every {interval:g}s (Ctrl+C to stop)", flush=True)
    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("# stopped", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in ("-w", "--watch"):
        interval = 2.0
        if len(args) >= 2:
            try:
                interval = float(args[1])
            except ValueError:
                interval = 2.0
        return _watch(interval)

    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    return _print_once()


if __name__ == "__main__":
    raise SystemExit(main())
