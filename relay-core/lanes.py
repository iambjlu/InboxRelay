# relay-core/lanes.py
"""背景工作的兩條「車道」與排程（第 12.3 節）。

- llm：外部 LLM（oMLX 上的 Qwen）— AI 分析、每日／月／年總結、記憶升級。
- ane：本機 Apple Neural Engine（選單列 App 用 Vision 做圖片文字辨識）。
- shot：連結截圖（本機 WebKit，獨立排程，不跟著 llm；連結摘要仍屬 llm）。

每條車道各自有排程模式、開始／結束時間、立即執行、暫停／繼續：
- always  永遠立即執行（有工作就做；llm 每 10 分鐘檢查一次有沒有可以寫的每日／月／年總結，不用設開始時間）
- daily   每天自動開始到做完為止
- window  每天自動開始與暫停（開始時間開、結束時間停，沒做完的明天繼續）
- manual  手動執行（只有按「立即執行」才做，做完就停）
互動功能（搜尋、實驗頻道、通知頻道）不受車道限制。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dtime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("lanes")

MODES = ("always", "daily", "window", "manual")
ALWAYS_CHECK_SEC = 600      # 「永遠立即執行」多久檢查一次總結
MODE_LABEL = {"always": "永遠立即執行", "daily": "每天自動開始到做完為止",
              "window": "每天自動開始與暫停", "manual": "手動執行"}


def parse_hm(s: str | None) -> dtime | None:
    if not s:
        return None
    try:
        h, m = (int(x) for x in s.strip().split(":"))
        return dtime(h, m)
    except ValueError:
        return None


class Lane:
    def __init__(self, name: str, label: str, mode: str = "always", start: str = "03:30",
                 end: str | None = None, tz: str = "Asia/Taipei") -> None:
        self.name, self.label = name, label
        self.mode = mode if mode in MODES else "always"
        self.start = parse_hm(start) or dtime(3, 30)
        self.end = parse_hm(end)
        if self.mode == "window" and self.end is None:
            self.mode = "daily"                 # 沒有結束時間就當「做到完」
        self.tz = ZoneInfo(tz)
        self.open = self.mode == "always"
        self.manual = False
        self.paused = False
        self.available = True                   # ane：App 沒連上時 False
        self.offline = False                    # llm：外部 LLM 連不上 → 這一輪先停，下次開始時間再試
        self.next_start: datetime | None = None
        self.close_at: datetime | None = None
        self._changed = asyncio.Event()
        self._trigger = asyncio.Event()

    # ------------------------------------------------------------ 狀態
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def allowed(self) -> bool:
        return self.available and not self.paused and not self.offline and (self.mode == "always" or self.open)

    @property
    def state(self) -> str:
        """paused／running（可以跑）／waiting（等時間）／unavailable。"""
        if not self.available:
            return "unavailable"
        if self.paused:
            return "paused"
        if self.offline:
            return "offline"
        return "running" if self.allowed() else "waiting"

    @property
    def queued(self) -> bool:
        return self._trigger.is_set()

    def notify(self) -> None:
        self._changed.set()

    async def gate(self) -> None:
        """背景工作在每個項目之前呼叫：暫停中或不在執行時段就在這裡等。"""
        while not self.allowed():
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------ 操作
    def go_offline(self, why: str = "") -> None:
        """外部 LLM 連不上：這一輪停下來，積欠的工作都留著，下次開始時間（永遠執行＝10 分鐘後）再試。"""
        if not self.offline:
            self.offline = True
            log.warning("🔌 %s：外部 LLM 連不上%s，沒做完的留到%s再試", self.label, f"（{why}）" if why else "",
                        "10 分鐘後" if self.mode == "always" else "下次開始時間")
            self.notify()

    def pause(self) -> None:
        if not self.paused:
            self.paused = True
            log.info("⏸  已暫停：%s", self.label)

    def resume(self) -> None:
        if self.paused:
            self.paused = False
            log.info("▶️  繼續：%s", self.label)
            self.notify()

    def run_now(self) -> bool:
        """立即執行：不管排程，馬上開始做到完（暫停中會一併繼續）。已在排隊回傳 False。"""
        if self._trigger.is_set():
            return False
        self.resume()
        if self.open and self.mode != "always":
            return True                         # 已經在執行時段裡了，繼續做就好
        self._trigger.set()
        self.notify()
        return True

    def describe(self) -> str:
        if self.mode == "always":
            return "永遠立即執行"
        if self.mode == "manual":
            return "手動執行"
        s = self.start.strftime("%H:%M")
        if self.mode == "window" and self.end:
            return f"每天 {s}～{self.end.strftime('%H:%M')}"
        return f"每天 {s} 開始到做完"

    def _next(self, t: dtime, after: datetime) -> datetime:
        x = after.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        return x if x > after else x + timedelta(days=1)


async def _wait_start_or_trigger(lane: Lane, use_schedule: bool) -> bool:
    """等到開始時間（use_schedule）或有人按立即執行；回傳是否為手動觸發。"""
    timeout = None
    if lane.mode == "always":
        lane.next_start = lane.now() + timedelta(seconds=ALWAYS_CHECK_SEC) if lane.offline else None
        timeout = ALWAYS_CHECK_SEC
    elif use_schedule:
        lane.next_start = lane._next(lane.start, lane.now())
        timeout = (lane.next_start - lane.now()).total_seconds()
    else:
        lane.next_start = None
    try:
        await asyncio.wait_for(lane._trigger.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def run_lane(lane: Lane, drained: Callable[[], bool],
                   cycle: Callable[[], Awaitable[None]] | None = None) -> None:
    """車道排程主迴圈。cycle：每次開始時要做的事（llm＝每日總結流程）；drained：工作是否都做完了。"""
    while True:
        manual = await _wait_start_or_trigger(lane, use_schedule=lane.mode != "manual")
        lane._trigger.clear()
        if lane.offline:
            log.info("🔌 %s：重新嘗試連線外部 LLM", lane.label)
        lane.offline = False                      # 新的一輪：再試一次外部 LLM
        if lane.mode == "always":
            lane.open = True
        else:
            lane.open, lane.manual = True, manual
            lane.close_at = (lane._next(lane.end, lane.now())
                             if lane.mode == "window" and lane.end and not manual else None)
            log.info("🚦 %s 開始（%s）%s", lane.label, "手動：立即執行" if manual else lane.describe(),
                     f"，{lane.close_at.strftime('%H:%M')} 暫停" if lane.close_at else "，做完為止")
        lane.notify()

        async def body() -> None:
            if cycle is not None:
                await cycle()
            while not drained():
                await asyncio.sleep(10)

        task = asyncio.create_task(body())
        reason = ""
        try:
            while True:
                if lane.offline:
                    reason = "外部 LLM 連不上，下次再試"
                    break
                if lane.close_at and lane.now() >= lane.close_at:
                    reason = "到結束時間，沒做完的下次繼續"
                    break
                if task.done():
                    if lane.close_at:                 # 時段內做完了：繼續開著接新工作，到結束時間才關
                        if not drained():
                            task = asyncio.create_task(body())
                        await asyncio.sleep(10)
                        continue
                    reason = "全部做完"
                    break
                await asyncio.sleep(5)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            elif task.exception() is not None:
                log.error("%s 執行失敗：%s", lane.label, task.exception())
            if lane.mode != "always":
                lane.open, lane.manual, lane.close_at = False, False, None
                log.info("🚦 %s 結束（%s）", lane.label, reason or "中斷")
            lane.notify()


# ------------------------------------------------------------ 全域
LANES: dict[str, Lane] = {}


def setup(cfg) -> dict[str, Lane]:
    LANES.clear()
    LANES["llm"] = Lane("llm", "外部 LLM 工作", cfg.llm_schedule, cfg.recap_time, cfg.llm_end, cfg.tz)
    LANES["ane"] = Lane("ane", "本機 ANE 文字辨識", cfg.ane_schedule, cfg.ane_start, cfg.ane_end, cfg.tz)
    LANES["shot"] = Lane("shot", "連結截圖", cfg.shot_schedule, cfg.shot_start, cfg.shot_end, cfg.tz)
    return LANES


def get(name: str) -> Lane | None:
    return LANES.get(name)
