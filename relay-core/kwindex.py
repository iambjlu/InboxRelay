# relay-core/kwindex.py
"""#關鍵字庫 的記憶體索引：(tg_chat, tg_msg) → (紀錄訊息 ID, KwRecord)。

- 啟動時讀完整個頻道（十幾萬筆也只佔幾 MB）。
- 之後每 5 分鐘只讀「上次之後」的新訊息（PC 匯入工具寫入的紀錄）。
- 補欄位時「編輯同一則訊息」，不另發新的。
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass

import discord

import records
from records import KwRecord

log = logging.getLogger("kwindex")

Key = tuple


@dataclass
class KwEntry:
    msg_id: int
    rec: KwRecord


class KwIndex:
    def __init__(self, channel: discord.TextChannel, bot_id: int) -> None:
        self.channel = channel
        self.bot_id = bot_id
        self.entries: dict[Key, KwEntry] = {}
        self.last_msg_id = 0

    def __contains__(self, key: Key) -> bool:
        return key in self.entries

    def get(self, key: Key) -> KwEntry | None:
        return self.entries.get(key)

    def keys(self) -> set:
        return set(self.entries)

    def _ingest(self, m: discord.Message) -> KwEntry | None:
        self.last_msg_id = max(self.last_msg_id, m.id)
        if m.author.id != self.bot_id:
            return None
        rec = records.parse_record(m.content)
        if rec is None:
            return None
        e = KwEntry(m.id, rec)
        self.entries[rec.key] = e
        return e

    async def load(self) -> None:
        self.entries.clear()
        n = 0
        async for m in self.channel.history(limit=None, oldest_first=True):
            self._ingest(m)
            n += 1
            if n % 1000 == 0:      # 幾千筆要讀 1～3 分鐘（Discord 一次只給 100 則），定期回報免得以為當掉
                log.info("#關鍵字庫 載入中… 已讀 %s 則", f"{n:,}")
        log.info("#關鍵字庫 載入 %d 筆（未分析 %d 筆）", len(self.entries),
                 sum(1 for e in self.entries.values() if not e.rec.enriched))

    async def poll_new(self) -> list[KwEntry]:
        """讀上次之後的新紀錄（例如 PC 匯入工具寫的）。"""
        new: list[KwEntry] = []
        after = discord.Object(id=self.last_msg_id) if self.last_msg_id else None
        async for m in self.channel.history(limit=None, after=after, oldest_first=True):
            known = records.parse_key(m.content)
            if known in self.entries:
                self.last_msg_id = max(self.last_msg_id, m.id)
                continue
            e = self._ingest(m)
            if e:
                new.append(e)
        return new

    def add(self, rec: KwRecord, msg: discord.Message) -> None:
        self.entries[rec.key] = KwEntry(msg.id, rec)
        self.last_msg_id = max(self.last_msg_id, msg.id)

    def by_mirror(self, mirror_url: str) -> list[KwEntry]:
        return sorted((e for e in self.entries.values() if e.rec.mirror == mirror_url),
                      key=lambda e: e.rec.tg_msg)

    async def update(self, key: Key, **changes) -> KwRecord | None:
        e = self.entries.get(key)
        if e is None:
            return None
        e.rec = dataclasses.replace(e.rec, **changes)
        await self.channel.get_partial_message(e.msg_id).edit(content=records.format_kw(e.rec))
        return e.rec

    async def delete(self, key: Key) -> None:
        e = self.entries.pop(key, None)
        if e is None:
            return
        try:
            await self.channel.get_partial_message(e.msg_id).delete()
        except discord.NotFound:
            pass
