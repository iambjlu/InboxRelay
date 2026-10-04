# relay-core/activity.py
"""外部 LLM 工作追蹤：現在在做什麼、進度多少；以及手動暫停背景工作。

- 終端機：每 15 秒（或進度跳 5%）印一行「⏳ AI 分析 120/7045（1.7%）」。
- Discord：有工作做超過 10 秒，就在 #關鍵字庫 發一則進度訊息並持續編輯；全部做完就刪掉。
- 暫停：背景工作（補分析、總結、記憶升級）在每個工作項目之間等待；搜尋、測試頻道等互動照常。
注意：asyncio.Event 要在事件迴圈裡建立（Python 3.9），所以由 main 呼叫 init()。
"""
from __future__ import annotations

import asyncio
import contextvars
import itertools
import logging
import time
from dataclasses import dataclass, field

import discord

import lanes

log = logging.getLogger("activity")

STATUS_HEAD = "⏳ **背景工作中（外部 LLM／本機 ANE）**"
DISCORD_AFTER_SEC = 10
DISCORD_EVERY_SEC = 15
TERMINAL_EVERY_SEC = 15


@dataclass
class Task:
    id: int
    label: str
    total: int | None = None
    done: int = 0
    detail: str = ""
    background: bool = True
    started: float = field(default_factory=time.monotonic)
    # 單一項目內的細部進度：一個項目分成幾個步驟（連結、圖片、關鍵字…），每步驟內再看 AI 已產生多少字
    item_steps: int = 0
    item_step: int = 0
    llm_frac: float = 0.0

    def begin_item(self, steps: int) -> None:
        self.item_steps, self.item_step, self.llm_frac = max(steps, 0), 0, 0.0

    def step(self) -> None:
        self.item_step += 1
        self.llm_frac = 0.0

    def set_llm(self, frac: float) -> None:
        self.llm_frac = max(0.0, min(frac, 0.95))

    @property
    def part(self) -> float:
        """目前這個項目做了幾成（0～<1）。"""
        if self.item_steps:
            return min((self.item_step + self.llm_frac) / self.item_steps, 0.99)
        return min(self.llm_frac, 0.99)

    @property
    def progress(self) -> float | None:
        """整體進度 0～1（含目前項目的細部進度）；沒有總數時是 None。"""
        if not self.total:
            return None
        return min((self.done + (self.part if self.done < self.total else 0)) / self.total, 1.0)

    def advance(self, n: int = 1, detail: str | None = None) -> None:
        self.done += n
        self.item_steps, self.item_step, self.llm_frac = 0, 0, 0.0
        if detail is not None:
            self.detail = detail

    def set(self, done: int | None = None, total: int | None = None, detail: str | None = None) -> None:
        if done is not None:
            if done != self.done:
                self.item_steps, self.item_step, self.llm_frac = 0, 0, 0.0
            self.done = done
        if total is not None:
            self.total = total
        if detail is not None:
            self.detail = detail

    def line(self) -> str:
        if self.total:
            p = self.progress or 0.0
            pct = 100 * p
            prog = f" {self.done:,}/{self.total:,}（{pct:.1f}%）"
            elapsed = time.monotonic() - self.started
            if 0.01 < p < 1 and elapsed > 30:
                eta = elapsed / p * (1 - p)
                h, rem = divmod(int(eta), 3600)
                prog += f"｜剩約 {h}h{rem // 60:02d}m" if h else f"｜剩約 {max(rem // 60, 1)} 分"
        else:
            prog = ""
        return f"{self.label}{prog}" + (f"｜{self.detail}" if self.detail else "")


class Activity:
    def __init__(self) -> None:
        self.tasks: dict[int, Task] = {}
        self._ids = itertools.count(1)
        self._status_msg: discord.Message | None = None
        self._last_discord = ""
        self._last_discord_at = 0.0
        self._last_term_at = 0.0
        self._last_term_pct: dict[int, int] = {}
        self.terminal_progress = True       # 有底部狀態列時就不另外印進度行
        self.mem = None                     # memguard.MemGuard（狀態列顯示記憶體用）

    # ------------------------------------------------------------ 工作
    def start(self, label: str, total: int | None = None, background: bool = True) -> Task:
        t = Task(next(self._ids), label, total, background=background)
        self.tasks[t.id] = t
        log.info("▶️  %s", t.line())
        return t

    def finish(self, t: Task, note: str = "") -> None:
        if self.tasks.pop(t.id, None) is not None:
            log.info("✅ %s 完成%s", t.label, f"（{note}）" if note else (f"（{t.done:,} 項）" if t.total else ""))
        self._last_term_pct.pop(t.id, None)
        if _bound.get() is t:
            _bound.set(None)

    # ------------------------------------------------------------ 暫停（＝外部 LLM 車道）
    @property
    def paused(self) -> bool:
        lane = lanes.get("llm")
        return bool(lane and lane.paused)

    def pause(self) -> None:
        lane = lanes.get("llm")
        if lane:
            lane.pause()

    def resume(self) -> None:
        lane = lanes.get("llm")
        if lane:
            lane.resume()

    async def gate(self, lane_name: str = "llm") -> None:
        """背景工作在每個項目之前呼叫；該車道暫停或不在執行時段時會在這裡等。"""
        lane = lanes.get(lane_name)
        if lane is not None:
            await lane.gate()

    def status_lines(self) -> list[str]:
        lines = [t.line() for t in sorted(self.tasks.values(), key=lambda t: t.id)]
        for ln in lanes.LANES.values():
            if ln.state == "paused":
                lines.insert(0, f"⏸ {ln.label}已暫停")
            elif ln.state == "offline":
                when = ln.next_start.strftime("%m/%d %H:%M") if ln.next_start else "下次開始時間"
                lines.insert(0, f"🔌 外部 LLM 連不上：沒做完的留著，{when} 再試")
            elif ln.state == "waiting":
                lines.insert(0, f"🌙 {ln.label}等待中（{ln.describe()}）")
        return lines

    # ------------------------------------------------------------ 顯示
    def _terminal(self) -> None:
        if not self.terminal_progress:
            return
        now = time.monotonic()
        jumped = False
        for t in self.tasks.values():
            if t.total:
                pct = int(100 * (t.progress or 0)) // 5
                if self._last_term_pct.get(t.id) != pct:
                    self._last_term_pct[t.id] = pct
                    jumped = True
        if self.tasks and (jumped or now - self._last_term_at >= TERMINAL_EVERY_SEC):
            self._last_term_at = now
            for ln in self.status_lines():
                log.info("⏳ %s", ln)

    async def _discord(self, channel) -> None:
        now = time.monotonic()
        long_running = [t for t in self.tasks.values() if now - t.started >= DISCORD_AFTER_SEC]
        if not long_running:
            if self._status_msg is not None and not self.tasks:
                try:
                    await self._status_msg.delete()
                except discord.HTTPException:
                    pass
                self._status_msg, self._last_discord = None, ""
            return
        body = "\n".join(f"• {ln}" for ln in self.status_lines())
        content = f"{STATUS_HEAD}\n{body}\n-# 完成後這則會自動刪除"
        if content == self._last_discord or now - self._last_discord_at < DISCORD_EVERY_SEC:
            return
        try:
            if self._status_msg is None:
                self._status_msg = await channel.send(content[:2000])
            else:
                await self._status_msg.edit(content=content[:2000])
            self._last_discord, self._last_discord_at = content, now
        except discord.NotFound:
            self._status_msg = None
        except discord.HTTPException as e:
            log.warning("更新進度訊息失敗：%s", e)

    async def cleanup_old(self, channel, bot_id: int) -> None:
        """刪掉上次沒刪到的進度訊息（例如程式當掉）。"""
        async for m in channel.history(limit=50):
            if m.author.id == bot_id and m.content.startswith((STATUS_HEAD, "⏳ **本地 AI 工作中**", "⏳ **外部 LLM 工作中**")):   # 含舊版標題
                try:
                    await m.delete()
                except discord.HTTPException:
                    pass

    async def reporter(self, channel) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                self._terminal()
                if channel is not None:
                    await self._discord(channel)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("進度顯示失敗")

    async def close(self) -> None:
        if self._status_msg is not None:
            try:
                await self._status_msg.delete()
            except discord.HTTPException:
                pass
            self._status_msg = None


current: Activity | None = None


def init() -> Activity:
    global current
    current = Activity()
    return current


class _NullTask:
    def advance(self, *a, **k) -> None: ...
    def set(self, *a, **k) -> None: ...
    def begin_item(self, *a, **k) -> None: ...
    def step(self, *a, **k) -> None: ...
    def set_llm(self, *a, **k) -> None: ...


# 目前協程正在替哪個工作做事（LLM 串流時用來回報「已產生多少字」）
_bound: contextvars.ContextVar = contextvars.ContextVar("activity_task", default=None)


def bind(t) -> None:
    """之後在這個協程（及它 await 的函式）裡呼叫 LLM，進度都算到 t。"""
    _bound.set(t if isinstance(t, Task) else None)


def bound() -> "Task | None":
    return _bound.get()


def step() -> None:
    t = _bound.get()
    if t is not None:
        t.step()


def begin_item(steps: int) -> None:
    t = _bound.get()
    if t is not None:
        t.begin_item(steps)


def start(label: str, total: int | None = None, background: bool = True):
    return current.start(label, total, background) if current else _NullTask()


def finish(t, note: str = "") -> None:
    if current and isinstance(t, Task):
        current.finish(t, note)


async def gate(lane_name: str = "llm") -> None:
    if current:
        await current.gate(lane_name)

