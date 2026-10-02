# relay-core/memguard.py
"""記憶體上限守門員：常駐在 8GB 電腦也不會越用越肥。

- 每分鐘量一次本程式的實際記憶體（RSS）。
- 超過軟上限（MEMORY_SOFT_MB，預設 600）：清掉可重建的快取（完整原文快取、測試頻道分析快取…）並回收記憶體。
- 連續 3 次超過硬上限（MEMORY_HARD_MB，預設 1000）：
  - 由 App 管理時：自行正常結束，App 會自動重新啟動（重新載入只需幾十秒，資料都在 Discord）。
  - 在終端機執行時：在通知頻道警告一次。
"""
from __future__ import annotations

import asyncio
import gc
import logging
from typing import Awaitable, Callable

log = logging.getLogger("memory_guard")


def rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        import resource
        import sys
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 1024 / 1024 if sys.platform == "darwin" else peak / 1024   # macOS 單位是 bytes


class MemGuard:
    def __init__(self, soft_mb: float, hard_mb: float, trims: list[Callable[[], None]],
                 notify: Callable[[str], Awaitable[None]] | None, managed_by_app: bool,
                 stop_event: asyncio.Event | None) -> None:
        self.soft = soft_mb
        self.hard = hard_mb
        self.trims = trims
        self.notify = notify
        self.managed = managed_by_app
        self.stop_event = stop_event
        self.current = rss_mb()
        self.peak = self.current
        self._over = 0
        self._warned = False

    def trim(self) -> float:
        before = rss_mb()
        for t in self.trims:
            try:
                t()
            except Exception:
                log.exception("清快取失敗")
        gc.collect()
        after = rss_mb()
        log.info("🧹 記憶體整理：%.0f MB → %.0f MB", before, after)
        return after

    async def loop(self, every: float = 60) -> None:
        while True:
            await asyncio.sleep(every)
            self.current = rss_mb()
            self.peak = max(self.peak, self.current)
            if self.current > self.soft:
                self.current = self.trim()
            if self.current > self.hard:
                self._over += 1
                log.warning("記憶體 %.0f MB 超過上限 %.0f MB（第 %d 次）", self.current, self.hard, self._over)
                if self._over >= 3:
                    if self.managed and self.stop_event is not None:
                        log.warning("記憶體持續過高，核心自行重新啟動（由 App 自動拉起）")
                        if self.notify:
                            await self.notify(f"♻️ 記憶體 {self.current:.0f} MB 持續超過上限，核心自動重新啟動")
                        self.stop_event.set()
                        return
                    if self.notify and not self._warned:
                        self._warned = True
                        await self.notify(f"⚠️ InboxRelay 記憶體 {self.current:.0f} MB 持續超過上限 {self.hard:.0f} MB，"
                                          "建議重新啟動（或改用選單列 App，會自動處理）")
            else:
                self._over = 0
