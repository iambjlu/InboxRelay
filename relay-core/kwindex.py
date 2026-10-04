# relay-core/kwindex.py
"""#關鍵字庫 的記憶體索引：(tg_chat, tg_msg) → (紀錄訊息 ID, KwRecord)。

- 啟動時讀完整個頻道（十幾萬筆也只佔幾 MB）。
- 之後每 5 分鐘只讀「上次之後」的新訊息（PC 匯入工具寫入的紀錄）。
- 補欄位時「編輯同一則訊息」，不另發新的。
- 圖中文字放在這張圖自己的 OCR 訊息（OCR|tg_chat|tg_msg，一張圖一則、最多塞滿約 1,960 字），
  主紀錄的 ocr 欄位只寫「見 OCR 訊息」；讀取時把文字接回紀錄。
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field

import discord

import records
from records import KwRecord

log = logging.getLogger("kwindex")

Key = tuple


@dataclass
class KwEntry:
    msg_id: int
    rec: KwRecord
    ocr_msg: int | None = None                                 # 這張圖的 OCR 訊息 ID
    legacy: bool = False                                       # 舊格式（文字寫在主紀錄裡），之後要搬到 OCR 訊息


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
        x = records.parse_ocr_message(m.content)
        if x is not None:                       # 這張圖的 OCR 訊息 → 文字接回主紀錄
            key, text = x
            e = self.entries.get(key)
            if e is not None:
                e.ocr_msg = m.id
                e._ocr_sent = m.content                                         # type: ignore[attr-defined]
                e.rec = dataclasses.replace(e.rec, ocr=text or e.rec.ocr)
            return None
        rec = records.parse_record(m.content)
        if rec is None:
            return None
        old = self.entries.get(rec.key)
        legacy = bool(rec.ocr) and rec.ocr not in records.OCR_MARKERS and rec.ocr != records.OCR_REF
        e = KwEntry(m.id, rec, legacy=legacy)
        if old is not None and old.msg_id == m.id and old.ocr_msg:
            e.ocr_msg = old.ocr_msg
            e.rec = dataclasses.replace(e.rec, ocr=old.rec.ocr)
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
            if known in self.entries and self.entries[known].msg_id >= m.id:
                self.last_msg_id = max(self.last_msg_id, m.id)
                continue                     # 已經有，或是自己寫的（同一則）
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
        if "ocr" in changes or e.legacy:
            await self._sync_ocr(e)
        e.legacy = False
        return e.rec

    async def _sync_ocr(self, e: KwEntry) -> None:
        """讓這張圖的 OCR 訊息和目前的圖中文字一致：沒有就發、有變就改、沒文字就刪。"""
        want = records.ocr_message(e.rec)
        if want is None:
            if e.ocr_msg:
                try:
                    await self.channel.get_partial_message(e.ocr_msg).delete()
                except discord.NotFound:
                    pass
                e.ocr_msg = None
            return
        if e.ocr_msg:
            if want != getattr(e, "_ocr_sent", None):
                await self.channel.get_partial_message(e.ocr_msg).edit(content=want)
        else:
            m = await self.channel.send(want)
            e.ocr_msg = m.id
            self.last_msg_id = max(self.last_msg_id, m.id)
        e._ocr_sent = want                                                  # type: ignore[attr-defined]

    async def delete(self, key: Key) -> None:
        e = self.entries.pop(key, None)
        if e is None:
            return
        for mid in [e.msg_id] + ([e.ocr_msg] if e.ocr_msg else []):
            try:
                await self.channel.get_partial_message(mid).delete()
            except discord.NotFound:
                pass
