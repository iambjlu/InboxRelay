# relay-core/album.py
"""相簿緩衝：同一個 media_group_id 的訊息等 2 秒後合併成一筆。

為了維持順序：只要來了別的訊息（非同一相簿），就先把緩衝中的相簿送出。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


class AlbumBuffer(Generic[T]):
    def __init__(self, emit: Callable[[list[T]], Awaitable[None]], delay: float = 2.0,
                 sort_key: Callable[[T], Any] | None = None) -> None:
        self._emit = emit
        self._delay = delay
        self._sort_key = sort_key
        self._pending: dict[str, list[T]] = {}
        self._timers: dict[str, asyncio.Task] = {}

    async def add(self, group_id: str | None, piece: T) -> None:
        if group_id is None:
            await self.flush_all()
            await self._emit([piece])
            return
        # 別的相簿還在等 → 先送出，避免順序錯亂
        for gid in [g for g in self._pending if g != group_id]:
            await self.flush(gid)
        self._pending.setdefault(group_id, []).append(piece)
        old = self._timers.pop(group_id, None)
        if old:
            old.cancel()
        self._timers[group_id] = asyncio.create_task(self._later(group_id))

    async def _later(self, group_id: str) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return
        self._timers.pop(group_id, None)  # 自己就是計時器，不要取消自己
        await self.flush(group_id)

    async def flush(self, group_id: str) -> None:
        timer = self._timers.pop(group_id, None)
        if timer and timer is not asyncio.current_task():
            timer.cancel()
        pieces = self._pending.pop(group_id, None)
        if pieces:
            if self._sort_key:
                pieces.sort(key=self._sort_key)
            await self._emit(pieces)

    async def flush_all(self) -> None:
        for gid in list(self._pending):
            await self.flush(gid)
