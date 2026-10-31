"""A 通道（宿主 ambient 环境快照）的契约测试。

不启动轮询线程、不连宿主、不读 SMTC：实例用 ``object.__new__`` 手工搭，
``push_message`` 换成记录 kwargs 的桩，SMTC 用 monkeypatch 打桩。
"""

from __future__ import annotations

import math
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from plugin.plugins import now_playing as np_mod
from plugin.plugins.now_playing import NowPlayingPlugin, _ambient_ttl_for_interval

_SOURCE = Path(np_mod.__file__)


class _RecordingLogger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def info(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - stub
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - stub
        pass

    def debug(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - stub
        pass

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.errors.append(msg)


def _make_plugin(
    *,
    interval_s: float = 5.0,
    chat_enabled: bool = False,
    raises: bool = False,
) -> Any:
    obj = object.__new__(NowPlayingPlugin)
    obj._lock = threading.Lock()
    obj._stop = threading.Event()
    obj._thread = None
    obj._last_fingerprint = None
    obj._last_push_monotonic = None
    obj._last_snapshot = None
    obj._tick_count = 0
    obj._push_count = 0
    obj._chat_push_count = 0
    obj._interval_s = interval_s
    obj._ambient_ttl_s = _ambient_ttl_for_interval(interval_s)
    obj._chat_context = chat_enabled
    obj._chat_ttl_s = 12.0
    obj._include_lyric = False
    obj.logger = _RecordingLogger()
    obj.calls = []

    def _push_message(**kwargs: Any) -> Any:
        if raises:
            raise RuntimeError("host push failed")
        obj.calls.append(kwargs)
        return object()

    obj.push_message = _push_message
    return obj


def _set_smtc(monkeypatch: pytest.MonkeyPatch, **snapshot: Any) -> None:
    payload = dict(snapshot)
    monkeypatch.setattr(np_mod.smtc, "get_now_playing", lambda: dict(payload))


_PLAYING = {
    "playing": True,
    "title": "晴天",
    "artist": "周杰伦",
    "source_app_id": "Spotify.exe",
}
_STOPPED = {"playing": False, "title": "", "artist": ""}


def test_playing_tick_pushes_exactly_one_ambient_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _make_plugin(interval_s=5.0, chat_enabled=False)
    _set_smtc(monkeypatch, **_PLAYING)

    plugin._tick()

    assert len(plugin.calls) == 1
    kwargs = plugin.calls[0]
    assert set(kwargs) == {"parts", "visibility", "ai_behavior", "coalesce_key", "metadata"}
    assert kwargs["visibility"] == []
    assert kwargs["ai_behavior"] == "blind"
    assert kwargs["coalesce_key"] == "ambient:now_playing"

    parts = kwargs["parts"]
    assert len(parts) == 1 and parts[0]["type"] == "text"
    assert parts[0]["text"]

    metadata = kwargs["metadata"]
    assert metadata["context_type"] == "ambient"
    ambient = metadata["ambient"]
    assert ambient["provider"] == "now_playing"
    assert ambient["priority"] == 0
    assert ambient["text"] == parts[0]["text"]
    assert _AMBIENT_MIN <= ambient["ttl_s"] <= _AMBIENT_MAX
    assert ambient["ttl_s"] > plugin._interval_s
    assert ambient["data"]["playing"] is True
    assert ambient["data"]["title"] == "晴天"
    assert ambient["data"]["artist"] == "周杰伦"
    assert ambient["data"]["source_app_id"] == "Spotify.exe"


_AMBIENT_MIN = 1.0
_AMBIENT_MAX = 3600.0


def test_source_only_uses_new_context_types() -> None:
    src = _SOURCE.read_text(encoding="utf-8")

    # 旧的 A 通道 context_type 取值彻底清零（代码与文档都不许再出现）。
    assert not re.search(r'context_type["\']?\s*[:=]+\s*["\']now_playing["\']', src)
    assert '"now_playing":' not in src.replace("ambient:now_playing", "")
    assert not re.search(r"\b_CONTEXT_TYPE\b", src)
    assert not re.search(r"\b_COALESCE_KEY\b", src)

    # 仅剩 ambient（A）与 now_playing_chat（B）两种取值。
    assert '"context_type": "ambient"' in src
    assert '_CHAT_CONTEXT_TYPE = "now_playing_chat"' in src
    values = set(re.findall(r'context_type["\']?\s*[:=]+\s*["\']([a-z_]+)["\']', src))
    assert values <= {"ambient", "now_playing_chat"}
    assert "now_playing" not in values


def test_playing_to_stopped_tick_pushes_playing_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _make_plugin(chat_enabled=False)
    _set_smtc(monkeypatch, **_PLAYING)
    plugin._tick()

    _set_smtc(monkeypatch, **_STOPPED)
    plugin._tick()

    assert len(plugin.calls) == 2
    last = plugin.calls[-1]
    ambient = last["metadata"]["ambient"]
    assert last["metadata"]["context_type"] == "ambient"
    assert ambient["data"]["playing"] is False
    assert "没有在播放" in ambient["text"]
    assert ambient["text"] == last["parts"][0]["text"]


def test_never_playing_pushes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _make_plugin(chat_enabled=True)
    _set_smtc(monkeypatch, **_STOPPED)

    plugin._tick()
    plugin._tick()

    assert plugin.calls == []
    assert plugin._push_count == 0
    assert plugin._chat_push_count == 0


def test_chat_channel_contract_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _make_plugin(interval_s=5.0, chat_enabled=True)
    _set_smtc(monkeypatch, **_PLAYING)

    plugin._tick()

    assert len(plugin.calls) == 2
    chat = [c for c in plugin.calls if c["ai_behavior"] == "read"]
    assert len(chat) == 1
    entry = chat[0]
    assert entry["metadata"]["context_type"] == "now_playing_chat"
    assert entry["metadata"]["expires_in_s"] > plugin._interval_s
    assert entry["coalesce_key"] == "now-playing-chat"
    assert entry["visibility"] == []
    assert entry["parts"][0]["text"]


@pytest.mark.parametrize(
    ("interval_s", "override_s", "expected"),
    [
        (5.0, 0.0, 30.0),
        (1.0, 0.0, 30.0),
        (60.0, 0.0, 180.0),
        (100000.0, 0.0, 3600.0),
        (5.0, 0.0, 30.0),
        (5.0, -5.0, 30.0),
        (5.0, float("nan"), 30.0),
        (5.0, "abc", 30.0),
    ],
)
def test_ambient_ttl_boundaries(
    interval_s: float, override_s: Any, expected: float
) -> None:
    ttl = _ambient_ttl_for_interval(interval_s, override_s)
    assert math.isfinite(ttl)
    assert _AMBIENT_MIN <= ttl <= _AMBIENT_MAX
    assert ttl == expected
    assert ttl > interval_s or interval_s >= _AMBIENT_MAX


def test_push_swallows_exceptions_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _make_plugin(chat_enabled=False, raises=True)
    payload = plugin._build_payload(dict(_PLAYING))

    assert plugin._push(payload) is None
    assert len(plugin.logger.errors) == 1
    assert "ambient" in plugin.logger.errors[0]
