# relay-core/state.py
"""#資料庫 的固定紀錄（第 7.6 節）。程式會「編輯原訊息」更新，而不是一直發新的。

格式：第一行是標籤（例如 IMPORT），之後每行 `key: value`。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import discord

LOCK_STALE = timedelta(minutes=5)
HOST_STALE = timedelta(minutes=3)


def this_host() -> str:
    import socket
    return socket.gethostname().removesuffix(".local")


def other_host_active(data: dict[str, str] | None, me: str, now: datetime | None = None) -> str | None:
    """LIVE 紀錄顯示另一台電腦在 3 分鐘內還有心跳 → 回傳那台的名稱。"""
    if not data or data.get("status") != "running" or data.get("host") in (None, "", me):
        return None
    try:
        hb = datetime.fromisoformat(data.get("heartbeat", ""))
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return data.get("host") if now - hb < HOST_STALE else None


def parse_record(content: str, tag: str) -> dict[str, str] | None:
    lines = content.split("\n")
    if not lines or lines[0].strip() != tag:
        return None
    out: dict[str, str] = {}
    for line in lines[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
    return out


def format_record(tag: str, data: dict[str, str]) -> str:
    return "\n".join([tag] + [f"{k}: {v}" for k, v in data.items()])[:2000]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def import_lock_active(data: dict[str, str] | None, now: datetime | None = None) -> bool:
    """IMPORT.status 為 running 且 heartbeat 在 5 分鐘內 → 有人正在匯入。"""
    if not data or data.get("status") != "running":
        return False
    try:
        hb = datetime.fromisoformat(data.get("heartbeat", ""))
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc)
    return now - hb < LOCK_STALE


class DbRecord:
    """#資料庫 裡的一則固定紀錄。"""

    def __init__(self, channel: discord.abc.Messageable, bot_user_id: int, tag: str) -> None:
        self.channel = channel
        self.bot_user_id = bot_user_id
        self.tag = tag
        self.message: discord.Message | None = None
        self.data: dict[str, str] = {}

    async def load(self, scan: int | None = None) -> dict[str, str] | None:
        async for m in self.channel.history(limit=scan):  # type: ignore[attr-defined]
            if m.author.id != self.bot_user_id:
                continue
            d = parse_record(m.content, self.tag)
            if d is not None:
                self.message, self.data = m, d
                return d
        return None

    async def merge(self, **fields: str) -> None:
        """只更新部分欄位，其他保留。"""
        data = dict(self.data)
        data.update({k: str(v) for k, v in fields.items()})
        await self.save(data)

    async def save(self, data: dict[str, str]) -> None:
        self.data = data
        content = format_record(self.tag, data)
        if self.message is None:
            self.message = await self.channel.send(content)
        else:
            self.message = await self.message.edit(content=content)


class DirtyDays:
    """需要重新總結的日期（24 小時內有訊息被編輯或刪除），給 M5 用。存在 #資料庫 的 RECAP_DIRTY。"""

    def __init__(self, record: DbRecord) -> None:
        self.record = record
        self.dates: set[str] = set()

    async def load(self) -> None:
        d = await self.record.load() or {}
        self.dates = {x.strip() for x in d.get("dates", "").split(",") if x.strip()}

    async def mark(self, date: str) -> None:
        if date in self.dates:
            return
        self.dates.add(date)
        await self.record.save({"dates": ", ".join(sorted(self.dates)), "updated": now_iso()})

    async def clear(self, date: str) -> None:
        if date in self.dates:
            self.dates.discard(date)
            await self.record.save({"dates": ", ".join(sorted(self.dates)), "updated": now_iso()})
