# relay-core/kwindex.py
"""#關鍵字庫 的記憶體索引：(tg_chat, tg_msg) → (紀錄訊息 ID, KwRecord)。

- 啟動時讀完整個頻道（十幾萬筆也只佔幾 MB）。
- 之後每 5 分鐘只讀「上次之後」的新訊息（PC 匯入工具寫入的紀錄）。
- 補欄位時「編輯同一則訊息」，不另發新的。
- 圖中文字放在這張圖自己的 OCR 訊息（OCR|tg_chat|tg_msg，一張圖一則、最多塞滿約 1,960 字），
  主紀錄的 ocr 欄位只寫「見 OCR 訊息」；讀取時把文字接回紀錄。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass, field

import discord

import records
from records import KwRecord

log = logging.getLogger("kwindex")

Key = tuple


class _Gap:
    """編輯間隔會自動調整：discord.http 回報被限流（429）就拉長，之後順利時慢慢縮回去。"""
    value = 1.5
    bumped = False


class _RateWatch(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "rate limited" in msg and "PATCH" in msg:
            _Gap.value = min(_Gap.value * 1.5 + 0.5, 10.0)
            _Gap.bumped = True


logging.getLogger("discord.http").addHandler(_RateWatch())

EDIT_GAP_SEC = 1.5      # 同一頻道的編輯最短間隔（OCR／截圖兩條車道共用）；被限流（429）時 _Gap 會自動拉長到最多 10 秒，順利時慢慢縮回


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
        self._edit_lock = asyncio.Lock()
        self._last_edit = 0.0

    async def _edit(self, message_id: int, content: str) -> None:
        """編輯訊息：所有編輯排隊、每次至少間隔 EDIT_GAP_SEC（速度由這裡統一控制）。"""
        async with self._edit_lock:
            wait = self._last_edit + max(EDIT_GAP_SEC, _Gap.value) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            _Gap.bumped = False
            try:
                await self.channel.get_partial_message(message_id).edit(content=content)
            finally:
                self._last_edit = time.monotonic()
                if not _Gap.bumped:
                    _Gap.value = max(EDIT_GAP_SEC, _Gap.value * 0.97)

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
        old = records.format_kw(e.rec)
        e.rec = dataclasses.replace(e.rec, **changes)
        new = records.format_kw(e.rec)
        if new != old or e.legacy:                       # 內容沒變就不編輯，少打一次 API（舊格式一定要改寫）
            await self._edit(e.msg_id, new)
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
                await self._edit(e.ocr_msg, want)
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
