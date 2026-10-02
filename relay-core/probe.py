# relay-core/probe.py
"""刪除同步（第 7.2 節）：搬移後等 5 分鐘，對原訊息 copyMessage 到探測頻道。

- 成功 → 刪掉探測複本，放行後續分析。
- 「message to copy not found」→ 原訊息已刪，同步刪除 Discord 鏡像與紀錄。
- 其他錯誤（例如這類訊息不能被複製）→ 視為仍存在，照常分析。
只對實時訊息做；匯入的歷史訊息不做。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from telegram.error import BadRequest, RetryAfter, TelegramError

log = logging.getLogger("probe")

Key = tuple
OnResult = Callable[[list, str], Awaitable[None]]


def is_deleted_error(e: Exception) -> bool:
    return isinstance(e, BadRequest) and "not found" in str(e).lower()


class Prober:
    def __init__(self, bot, probe_chat_id: int, delay: float,
                 on_alive: OnResult, on_deleted: Callable[[list, str, bool], Awaitable[None]]) -> None:
        self.bot = bot
        self.probe_chat_id = probe_chat_id
        self.delay = delay
        self.on_alive = on_alive
        self.on_deleted = on_deleted
        self._sem = asyncio.Semaphore(1)
        self._tasks: set[asyncio.Task] = set()

    def schedule(self, keys: list, mirror_url: str, due: float | None = None) -> None:
        """due：monotonic 時間；None = 現在起算 delay 秒。"""
        wait = self.delay if due is None else max(0.0, due - time.monotonic())
        t = asyncio.create_task(self._run(keys, mirror_url, wait))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def _run(self, keys: list, mirror_url: str, wait: float) -> None:
        try:
            await asyncio.sleep(wait)
            async with self._sem:
                alive, deleted = [], []
                for k in keys:
                    (deleted if await self.probe(k) == "deleted" else alive).append(k)
                    await asyncio.sleep(0.3)
            if deleted:
                await self.on_deleted(deleted, mirror_url, len(deleted) == len(keys))
            if alive:
                await self.on_alive(alive, mirror_url)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("刪除同步探測失敗：%s", mirror_url)

    async def probe(self, key: Key) -> str:
        chat_id, msg_id = key
        for _ in range(3):
            try:
                copied = await self.bot.copy_message(chat_id=self.probe_chat_id, from_chat_id=chat_id,
                                                     message_id=msg_id, disable_notification=True)
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 5)) + 1)
                continue
            except TelegramError as e:
                if is_deleted_error(e):
                    log.info("TG #%s 已被刪除", msg_id)
                    return "deleted"
                log.info("TG #%s 無法探測（%s），視為仍存在", msg_id, e)
                return "unknown"
            try:
                await self.bot.delete_message(chat_id=self.probe_chat_id, message_id=copied.message_id)
            except TelegramError as e:
                log.warning("刪除探測複本失敗：%s", e)
            return "alive"
        return "unknown"

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
