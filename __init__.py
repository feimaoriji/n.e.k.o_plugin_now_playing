"""正在播放 · now_playing

把「此刻用户电脑在放什么」（Windows 系统媒体会话 SMTC / GSMTC）实时喂给
N.E.K.O 的自主搭话链路 —— 纯事实：歌名 / 歌手（可选：当前歌词行）。
不触发搭话，只在她自己开口那一刻作为背景信息可用。

数据通路（与宿主 ambient 环境快照契约一致，见
``docs/plugins/sdk-reference.md`` 的 Ambient context 一节）::

    本插件 ─┬─ 心跳 A（环境快照）：push_message(visibility=[],
            │   行为=blind, coalesce_key="ambient:now_playing",
            │   metadata={"context_type": "ambient", "ambient": {...}})
            │   ──> 宿主 EventBus：context_type == "ambient" 早退分支
            │   --> core.proactive.note_ambient_context()（同 provider 覆盖）
            │   --> 自主搭话 Phase 2 prompt 末尾拼「此刻在播放《X》— Y」
            │
            └─ 心跳 B（文本回复）：push_message(行为=read,
                metadata.context_type="now_playing_chat",
                metadata.expires_in_s=<TTL>) ──> 正常的 passive 队列
                --> 下一个**用户文本轮**被 drain，随 user message 一起进
                    prompt（渲染成「======[系统通知] 来自 now_playing 的消息======」）
                --> 于是她**回话时**也知道此刻在放什么

两条心跳互不干扰：
  * A 走宿主 ambient 环境快照通道（不落队、不渲染、不触发搭话），只服务自主搭话；
  * B 是 NEKO 原生 passive 链路（落队 → 用户轮 drain），只服务文本回复。
    B 的 ``context_type`` 是 ``now_playing_chat``，与 A 的 ``ambient`` 互不干扰。
  * B 带 ``expires_in_s`` 让队列里的旧快照自动过期：心跳每 5s 刷新一条，
    所以任意时刻队列里那条都 ≤5s；播放一停，最后一条在 TTL 内自动消失，
    不会拿一首早就不放了的歌去污染后面的对话。

契约要点（改这块之前必须对齐）：
  * A（环境快照）：
    - ``metadata.context_type="ambient"``，``metadata.ambient`` 含
      ``provider="now_playing"``（固定字面量）、``ttl_s``、``priority=0``、
      ``text``、``data={playing,title,artist,source_app_id[,lyric]}``；
    - ``ambient.text`` 与 ``parts[0]["text"]`` 必须完全一致（宿主两处都会读）；
    - ``visibility=[]`` —— 不在聊天窗冒系统气泡；
    - 行为=blind —— 只为**旧宿主**兜底（旧宿主把 blind 映射成 silent，事件不进
      LLM）；新宿主早退只看 ``metadata.context_type``，不看行为字段；
    - ``coalesce_key="ambient:now_playing"`` —— 心跳去重，只留最新一条；
    - ``ttl_s = max(3×轮询间隔, 30s)``，再夹到 ``[1, 3600]``（与宿主
      ``_AMBIENT_TTL_MIN_S/_MAX_S`` 一致）。ttl 必须 > 轮询间隔，
      否则两次心跳之间会出现「事实已过期」的空窗。
  * B（文本回复）：
    - ``visibility=[]``；行为=read —— 派生 passive，**绝不触发她开口**；
    - ``metadata.context_type="now_playing_chat"``（绝不能是 A 的 ``ambient``）、
      ``metadata.expires_in_s=<TTL>``；
    - ``coalesce_key="now-playing-chat"`` —— 只留最新一条；
    - TTL 必须 > 轮询间隔，否则用户开口时永远刚好过期；
      默认 ``max(2×间隔+2s, 8s)``（5s 间隔 → 12s），夹在 [8s, 60s]。

为什么用后台线程而不是 asyncio 任务：
    SDK 经**临时 asyncio.run()** 调用 ``@lifecycle(id="startup")``
    (plugin/core/host.py)，handler 一返回循环就关，底下任何
    ``asyncio.create_task`` 立刻被取消（game_agent_minecraft 踩过这个坑）。
    本插件全是同步 IO —— SMTC 同步读、urllib 同步网络、push_message 同步
    ZMQ —— 所以直接开一条 daemon 线程跑轮询即可：线程不绑任何 event loop，
    完全绕开该问题。

────────────────────────────────────────────────────────────────────────
注册方式：装饰器已启用（``@neko_plugin`` + ``@lifecycle`` / ``@plugin_entry``），
   宿主按 ``plugin.toml`` 的 entry 加载本类。
────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    lifecycle,
    neko_plugin,
    plugin_entry,
)

from . import lyrics as lyrics_reader
from . import smtc

# ---------------------------------------------------------------------------
# 常量 / 可调项（都可用环境变量覆盖；见 _env_float / _env_bool）
# ---------------------------------------------------------------------------
_DEFAULT_INTERVAL_S = 5.0  # 轮询间隔
_MIN_INTERVAL_S = 1.0
# 环境快照（A）：宿主 ambient 契约的固定字段与边界（以宿主 _AMBIENT_* 为准）。
_AMBIENT_PROVIDER = "now_playing"
_AMBIENT_COALESCE_KEY = "ambient:now_playing"
_AMBIENT_PRIORITY = 0
_AMBIENT_TTL_MIN_S = 1.0
_AMBIENT_TTL_MAX_S = 3600.0
_AMBIENT_TTL_DEFAULT_S = 30.0
_AMBIENT_TTL_INTERVAL_FACTOR = 3.0
# 「文本回复」副通道：走 NEKO 原生 passive 队列，用户开口那轮被 drain。
_CHAT_COALESCE_KEY = "now-playing-chat"
_CHAT_CONTEXT_TYPE = "now_playing_chat"
_CHAT_TTL_MIN_S = 8.0
_CHAT_TTL_MAX_S = 60.0
_TITLE_MAX = 200
_ARTIST_MAX = 200
_LYRIC_MAX = 120
_APP_ID_MAX = 120
_THREAD_NAME = "now_playing-poller"


def _env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on", "y"}:
        return True
    if raw in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _collapse(value: Any) -> str:
    """把任意值折叠成干净的、内部无多余空白的单行字符串。"""

    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def _chat_ttl_for_interval(interval_s: float, override_s: float = 0.0) -> float:
    """文本回复通道（B）的过期时间：必须 > 心跳间隔，且别长到留下陈旧歌名。

    默认 ``2×间隔 + 2s``（5s 间隔 → 12s），夹在 [8s, 60s] 之间；
    显式覆盖（``NOW_PLAYING_CHAT_TTL_S``）同样夹紧。
    """

    raw = override_s if override_s > 0 else interval_s * 2.0 + 2.0
    return min(max(raw, _CHAT_TTL_MIN_S), _CHAT_TTL_MAX_S)


def _ambient_ttl_for_interval(interval_s: float, override_s: float = 0.0) -> float:
    """环境快照（A）的过期时间：必须 > 轮询间隔。

    宿主 ``get_ambient_contexts`` 只认 ``age < ttl`` 的快照 —— 一条超过自身
    TTL 的记录会被当作「事实已过期」直接丢弃。心跳每个轮询间隔才刷新一次，
    所以 TTL 若 ≤ 间隔，两次心跳之间就会出现一个空窗：宿主在这段时间读到
    「没在放」，而她本来该看到正在播放。因此基准取 ``max(3×间隔, 30s)``，
    再夹到 ``[1, 3600]``（与宿主 ``_AMBIENT_TTL_MIN_S/_MAX_S`` 一致）。

    显式覆盖（``NOW_PLAYING_AMBIENT_TTL_S``）只在是有限正数时生效；
    ``0`` / 负数 / ``NaN`` / 非数一律落回计算值，绝不让 TTL 变成 0 或 inf。
    """

    try:
        base = float(interval_s)
    except (TypeError, ValueError):
        base = _DEFAULT_INTERVAL_S
    if not math.isfinite(base) or base <= 0:
        base = _DEFAULT_INTERVAL_S
    computed = max(_AMBIENT_TTL_INTERVAL_FACTOR * base, _AMBIENT_TTL_DEFAULT_S)

    try:
        override = float(override_s)
    except (TypeError, ValueError):
        override = 0.0
    raw = override if math.isfinite(override) and override > 0 else computed
    return min(max(raw, _AMBIENT_TTL_MIN_S), _AMBIENT_TTL_MAX_S)


@neko_plugin
class NowPlayingPlugin(NekoPluginBase):
    """把当前系统播放内容持续喂给 NEKO 的自主搭话链路（只读，不控制播放器）。"""

    def __init__(self, ctx):
        super().__init__(ctx)
        self.logger = ctx.logger

        # 轮询线程 + 共享状态
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_fingerprint: tuple[Any, ...] | None = None
        self._last_push_monotonic: float | None = None
        self._last_snapshot: dict[str, Any] | None = None
        self._tick_count = 0
        self._push_count = 0
        self._chat_push_count = 0

        interval = _env_float("NOW_PLAYING_INTERVAL_S", _DEFAULT_INTERVAL_S)
        self._interval_s = interval if interval >= _MIN_INTERVAL_S else _DEFAULT_INTERVAL_S
        # 环境快照（A）：TTL 必须 > 轮询间隔，否则两次心跳之间会空窗。
        self._ambient_ttl_s = _ambient_ttl_for_interval(
            self._interval_s,
            _env_float("NOW_PLAYING_AMBIENT_TTL_S", 0.0),
        )
        # 文本回复通道（B）：默认开。关掉只影响「回话时知不知道在放歌」，
        # 自主搭话那条 A 链不受影响。
        self._chat_context = _env_bool("NOW_PLAYING_CHAT_CONTEXT", True)
        self._chat_ttl_s = _chat_ttl_for_interval(
            self._interval_s,
            _env_float("NOW_PLAYING_CHAT_TTL_S", 0.0),
        )
        # 歌词：宿主 ambient 契约只承诺 text/data 透传给 prompt，默认关闭。
        # 打开后歌词会随 ambient.data.lyric 一起推上去（B 通道不单独带歌词）。
        self._include_lyric = _env_bool("NOW_PLAYING_INCLUDE_LYRIC", False)

    # ------------------------------------------------------------------
    # 状态构造 / 组装
    # ------------------------------------------------------------------
    def _build_payload(self, snapshot: Any) -> dict[str, Any]:
        """把 smtc 的原始快照规整成对外统一的 payload。"""

        raw: dict[str, Any] = snapshot if isinstance(snapshot, dict) else {}
        title = _collapse(raw.get("title"))[:_TITLE_MAX]
        artist = _collapse(raw.get("artist"))[:_ARTIST_MAX]
        album = _collapse(raw.get("album"))[:_ARTIST_MAX]
        app_id = _collapse(raw.get("source_app_id"))[:_APP_ID_MAX]
        playing = bool(raw.get("playing")) and bool(title)

        payload: dict[str, Any] = {
            "playing": playing,
            "title": title if playing else "",
            "artist": artist if playing else "",
            "album": album if playing else "",
            "source_app_id": (app_id or None) if playing else None,
            "status": raw.get("status") if playing else None,
            "position_ms": raw.get("position_ms") if playing else None,
            "duration_ms": raw.get("duration_ms") if playing else None,
            "fetched_at": raw.get("fetched_at"),
            "lyric": "",
        }
        if playing:
            payload["lyric"] = self._current_lyric(payload)
        return payload

    def _current_lyric(self, payload: dict[str, Any]) -> str:
        if not self._include_lyric or not payload.get("title"):
            return ""
        try:
            line = lyrics_reader.get_current_lyric(
                payload["title"],
                payload.get("artist") or "",
                payload.get("position_ms"),
            )
        except Exception:  # 歌词是锦上添花，任何异常都不该影响主链路
            return ""
        return _collapse(line)[:_LYRIC_MAX]

    def _format_text(self, payload: dict[str, Any]) -> str:
        if not payload.get("playing"):
            return "当前电脑没有在播放音乐。"
        title = payload.get("title") or "未知曲目"
        artist = payload.get("artist") or ""
        text = f"当前电脑正在播放：《{title}》" + (f" — {artist}" if artist else "")
        lyric = payload.get("lyric") or ""
        if lyric:
            text += f"（此刻歌词：{lyric}）"
        return text

    def _push(self, payload: dict[str, Any]) -> Any:
        """推一条宿主 ambient 环境快照；失败只记日志，不抛。"""

        text = self._format_text(payload)
        data: dict[str, Any] = {
            "playing": bool(payload.get("playing")),
            "title": payload.get("title") or "",
            "artist": payload.get("artist") or "",
            "source_app_id": payload.get("source_app_id"),
        }
        if payload.get("lyric"):
            data["lyric"] = payload["lyric"]
        metadata = {
            "context_type": "ambient",
            "ambient": {
                "provider": _AMBIENT_PROVIDER,
                "ttl_s": self._ambient_ttl_s,
                "priority": _AMBIENT_PRIORITY,
                "text": text,
                "data": data,
            },
        }
        try:
            return self.push_message(
                parts=[{"type": "text", "text": text}],
                visibility=[],
                ai_behavior="blind",
                coalesce_key=_AMBIENT_COALESCE_KEY,
                metadata=metadata,
            )
        except Exception:
            self.logger.exception("[now_playing] ambient push_message 失败")
            return None

    def _push_chat_context(self, payload: dict[str, Any]) -> Any:
        """推一条「文本回复」用的 passive 快照（走 NEKO 原生 passive 队列）。

        与 :meth:`_push` 的区别：

        * ``context_type`` 换成 ``now_playing_chat`` —— 与 A 通道的 ambient
          快照是两个命名空间，正常落进 ``pending_agent_callbacks``，不会被
          宿主当成背景事实；
        * 带 ``expires_in_s`` —— 队列里的旧快照会自动过期，不会拿停播前的歌
          去污染很久之后的对话；
        * 只在**正在播放**时推 —— 停播无需专门推「没在放」，最后一条 TTL 到了
          自然消失。

        进队后在**下一个用户文本轮**被 drain，随 user message 一起进 prompt，
        所以文本聊天回话时也能看到此刻在放什么。
        """

        if not self._chat_context or not payload.get("playing"):
            return None
        metadata = {
            "context_type": _CHAT_CONTEXT_TYPE,
            "expires_in_s": self._chat_ttl_s,
        }
        try:
            return self.push_message(
                parts=[{"type": "text", "text": self._format_text(payload)}],
                ai_behavior="read",
                visibility=[],
                coalesce_key=_CHAT_COALESCE_KEY,
                metadata=metadata,
            )
        except Exception:
            self.logger.exception("[now_playing] chat 上下文 push_message 失败")
            return None

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        snapshot = smtc.get_now_playing()
        payload = self._build_payload(snapshot)
        fingerprint = (
            payload.get("playing"),
            payload.get("title"),
            payload.get("artist"),
            payload.get("lyric"),
        )

        with self._lock:
            previous = self._last_fingerprint
            self._last_fingerprint = fingerprint
            self._tick_count += 1

        playing = bool(payload.get("playing"))
        was_playing = bool(previous and previous[0])

        # 播放中 → 每次 tick 都推（刷新 ambient 快照，撑住 ttl）；
        # 从「在放」跌到「没在放」→ 推一条 playing=False；
        # 一直没在放 → 不推（宿主过期后自然当没在放）。
        if playing or was_playing:
            if self._push(payload) is not None:
                with self._lock:
                    self._last_push_monotonic = time.monotonic()
                    self._push_count += 1

        # 文本回复副通道：只在播放中推（停播靠 TTL 自然过期，不推「没在放」）。
        if playing and self._push_chat_context(payload) is not None:
            with self._lock:
                self._chat_push_count += 1

        with self._lock:
            self._last_snapshot = dict(payload)

    def _loop(self) -> None:
        self.logger.info(
            "[now_playing] 轮询线程启动：间隔 %.1fs，SMTC 后端=%s",
            self._interval_s,
            smtc.winrt_backend,
        )
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                self.logger.exception("[now_playing] tick 异常（已忽略，继续轮询）")
            self._stop.wait(self._interval_s)
        self.logger.info("[now_playing] 轮询线程退出")

    def _start_monitor(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            thread = threading.Thread(target=self._loop, name=_THREAD_NAME, daemon=True)
            self._thread = thread
        thread.start()
        return True

    def _stop_monitor(self) -> bool:
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is None:
            return False
        self._stop.set()
        thread.join(timeout=2.0)
        return True

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _public_view(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        data = payload or {}
        return {
            "playing": bool(data.get("playing")),
            "title": data.get("title") or "",
            "artist": data.get("artist") or "",
            "album": data.get("album") or "",
            "lyric": data.get("lyric") or "",
            "status": data.get("status"),
            "position_ms": data.get("position_ms"),
            "duration_ms": data.get("duration_ms"),
            "source_app_id": data.get("source_app_id"),
            "fetched_at": data.get("fetched_at"),
        }

    # ------------------------------------------------------------------
    # 生命周期（startup 只做轻量活：起线程 + 记日志，绝不在里面 create_task）
    # ------------------------------------------------------------------
    @lifecycle(id="startup")
    async def startup(self, **_):
        backend = smtc.winrt_backend
        if backend is None:
            self.logger.warning(
                "[now_playing] 未检测到 WinRT 后端（winrt-* / winsdk 未安装）：%s；"
                "插件仍会加载，但读不到播放状态。",
                smtc.winrt_error,
            )
        else:
            self.logger.info("[now_playing] WinRT 后端 = %s", backend)

        started = False
        if _env_bool("NOW_PLAYING_AUTOSTART", True):
            started = self._start_monitor()
            self.logger.info("[now_playing] 自动启动轮询：%s", started)
        return Ok(
            {
                "status": "ready",
                "winrt_backend": backend,
                "interval_s": self._interval_s,
                "monitoring": started or self.is_running(),
            }
        )

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        self._stop_monitor()
        return Ok(
            {
                "status": "shutdown",
                "pushed": self._push_count,
                "chat_pushed": self._chat_push_count,
                "ticks": self._tick_count,
            }
        )

    # ------------------------------------------------------------------
    # 入口（手动控制 / 调试）
    # ------------------------------------------------------------------
    @plugin_entry(id="status", name="当前播放", description="查看此刻电脑在播放什么（读一次快照，不推消息）")
    async def status(self, refresh: bool = True, **_):
        if refresh:
            try:
                payload = self._build_payload(smtc.get_now_playing())
            except Exception as exc:
                return Err({"code": "read_failed", "message": str(exc)})
            with self._lock:
                self._last_snapshot = dict(payload)
        else:
            with self._lock:
                payload = dict(self._last_snapshot) if self._last_snapshot else None
        view = self._public_view(payload)
        view["monitoring"] = self.is_running()
        view["chat_context"] = bool(self._chat_context)
        view["chat_ttl_s"] = self._chat_ttl_s
        with self._lock:
            last_push = self._last_push_monotonic
            view["chat_pushes"] = self._chat_push_count
        # 距上次成功推送过了多久 —— 心跳是否真的在推，一眼可见（应 < 30s）
        view["last_push_age_s"] = round(time.monotonic() - last_push, 1) if last_push else None
        return Ok(view)

    # ── A+B 防呆（2026-10-19）：两条心跳同时启用，互不干扰 ──────────────────
    # 背景：10-19 21:33 她把一次 now_playing.start 误触发，随后把「插件调用成功」
    # 讲成「歌单更新好了、《X》躺进心动列表」—— 纯幻觉。两道闸一起上：
    #   A 防呆：description 明说「通常无需调用、默认已自动开启」，压低误触发；
    #          已在跑时不再"成功启动"，并在日志留痕。
    #   B 语义：返回值带 reply（parse_plugin_result 无 llm_result_fields 时直接取
    #          run_data["reply"] 作为任务结果文本给到她），把"这不是歌单操作"钉死。
    _REPLY_STARTED = (
        "监听已开启，我会继续读当前播放。注意：这只是监听开关，"
        "不等于歌单操作，歌单和收藏一点没动。"
    )
    _REPLY_ALREADY = (
        "监听本来就在跑啦，不用再按 START —— 这一步只是确认「正在播放」的监听状态，"
        "没有对歌单、收藏或任何播放列表做任何改动。"
    )
    _REPLY_STOPPED = "监听已停止，我不再读当前播放了（同样只是监听开关，没动歌单）。"
    _REPLY_NOT_MONITORING = "监听本来就没在跑，不用停啦。"

    @plugin_entry(
        id="start",
        name="开始监听",
        description=(
            "（通常无需调用：后台监听默认已自动开启）仅在明确要求重启监听时使用；"
            "只控制监听开关，不涉及歌单/收藏"
        ),
    )
    async def start(self, **_):
        started = self._start_monitor()
        monitoring = self.is_running()
        reply = self._REPLY_STARTED if started else self._REPLY_ALREADY
        self.logger.info(
            "[now_playing] start 入口被调用：started=%s monitoring=%s（仅监听开关，不涉及歌单）",
            started, monitoring,
        )
        return Ok(
            {
                "started": started,
                "monitoring": monitoring,
                "affects_playlist": False,
                "reply": reply,
            }
        )

    @plugin_entry(id="stop", name="停止监听", description="停止后台轮询（不再推送当前播放）")
    async def stop(self, **_):
        stopped = self._stop_monitor()
        monitoring = self.is_running()
        self.logger.info("[now_playing] stop 入口被调用：stopped=%s monitoring=%s", stopped, monitoring)
        return Ok(
            {
                "stopped": stopped,
                "monitoring": monitoring,
                "reply": self._REPLY_STOPPED if stopped else self._REPLY_NOT_MONITORING,
            }
        )

    @plugin_entry(id="push_now", name="立即推送", description="立刻读一次并推一条 now_playing 事件（调试用）")
    async def push_now(self, **_):
        payload = self._build_payload(smtc.get_now_playing())
        receipt = self._push(payload)
        return Ok(
            {
                "submitted": receipt is not None,
                "pushed": self._push_count,
                "snapshot": self._public_view(payload),
            }
        )
