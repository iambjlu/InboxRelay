# relay-core/relay.py
"""實時搬移（第 7.1 節）＋刪除同步（7.2）＋編輯同步（7.2.1）。

- 附件一律在記憶體（bytes）處理，不落地。
- 超過上限 min(20MB, 伺服器上限) 的附件只貼檔名。
- 去重：已在 #關鍵字庫 出現過的 (tg_chat, tg_msg) 直接跳過。
- 搬完後交給刪除同步（5 分鐘後探測）；確認還在才送去 AI 分析。
"""
from __future__ import annotations

import asyncio
import io
import logging
from collections import OrderedDict
from datetime import datetime, timezone

import discord
from telegram.error import BadRequest, TelegramError

import formatting
import records
from config import Config
from dc_output import DiscordOut
from kwindex import KwIndex
from tg_intake import AttachmentRef, RelayItem, TgPiece

log = logging.getLogger("relay")

TEXT_CACHE_MAX = 1000   # 最近 1000 則的完整原文（每則最多約 4KB）


def plan_groups(atts: list, limit: int) -> list[list]:
    """依 Telegram 提供的檔案大小分批（大小未知的先估一半上限）。"""
    groups: list[list] = []
    cur: list = []
    size = 0
    from enrich.image import is_heic_name
    for a in atts:
        est = a.file_size or limit // 2
        if is_heic_name(a.file_name):             # HEIC 會另附一份 JPEG：自己一批，容量才夠放兩份
            if cur:
                groups.append(cur)
                cur, size = [], 0
            groups.append([a])
            continue
        if cur and (len(cur) == 10 or size + est > limit):
            groups.append(cur)
            cur, size = [], 0
        cur.append(a)
        size += est
    if cur:
        groups.append(cur)
    return groups


class TextCache(OrderedDict):
    """key → 完整原文（只留最近的幾千則；重啟後改用 #關鍵字庫 的 excerpt）。"""

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > TEXT_CACHE_MAX:
            self.popitem(last=False)


class Relayer:
    def __init__(self, cfg: Config, dc: DiscordOut, bot, queue: "asyncio.Queue[RelayItem]") -> None:
        self.cfg = cfg
        self.dc = dc
        self.bot = bot
        self.queue = queue
        self.index: KwIndex | None = None
        self.text_cache: TextCache = TextCache()
        self.last_header_date: str | None = None
        self.prober = None      # probe.Prober
        self.enricher = None    # enrich.pipeline.Enricher
        self.dirty = None       # state.DirtyDays
        self.lock = asyncio.Lock()   # 實時搬移與反向同步輪流處理，避免順序錯亂
        self.reverse = None     # dc_reverse.ReverseSync
        self.ocr = None         # ocr.OcrWorker（選單列 App 管理時才有）

    async def load_state(self) -> None:
        self.last_header_date = await self.dc.last_header_date(self.cfg.ch_inbox_mirror)
        ch = await self.dc.channel(self.cfg.ch_keywords)
        log.info("讀取 #關鍵字庫（紀錄多時要 1～3 分鐘）…")
        self.index = KwIndex(ch, self.dc.client.user.id)  # type: ignore[arg-type,union-attr]
        await self.index.load()
        log.info("最後的日期標頭：%s；#關鍵字庫 已有 %d 筆紀錄",
                 self.last_header_date or "（無）", len(self.index.entries))

    @property
    def seen(self) -> set:
        return self.index.keys() if self.index else set()

    async def run(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                async with self.lock:
                    if item.edit:
                        await self.handle_edit(item.first)
                    else:
                        await self.handle(item)
            except Exception:
                log.exception("處理 TG #%s 失敗", item.first.tg_msg)
            finally:
                self.queue.task_done()

    # ------------------------------------------------------------------ 下載
    async def _download(self, att: AttachmentRef, limit: int) -> bytes | None:
        """下載到記憶體；過大回傳 None。"""
        if att.file_size and att.file_size > limit:
            return None
        try:
            f = await self.bot.get_file(att.file_id)
        except BadRequest as e:
            if "too big" in str(e).lower():  # Bot API 下載上限 20MB
                return None
            raise
        if f.file_size and f.file_size > limit:
            return None
        data = bytes(await f.download_as_bytearray())
        return None if len(data) > limit else data

    async def _add_jpeg_copy(self, files: list, name: str, data: bytes, limit: int) -> None:
        """HEIC 另附一份 JPEG（和原檔同一則；兩份加起來不超過上限）。轉不出來就只留原檔。"""
        from enrich.image import MIN_ROOM, heic_to_jpeg, is_heic_name
        if not is_heic_name(name) or limit - len(data) < MIN_ROOM:
            return
        jpg = await asyncio.to_thread(heic_to_jpeg, data, limit - len(data))
        if jpg is None:
            log.warning("HEIC 轉 JPEG 失敗，只搬原檔：%s", name)
            return
        files.append((name + ".jpg", jpg))

    async def _collect_files(self, pieces: list[TgPiece], limit: int) -> tuple[list, list]:
        files: list[tuple[str, bytes]] = []
        oversize: list[str] = []
        for p in pieces:
            if p.attachment is None:
                continue
            try:
                data = await self._download(p.attachment, limit)
            except TelegramError as e:
                log.warning("下載 %s 失敗：%s，改貼檔名", p.attachment.file_name, e)
                data = None
            if data is None:
                oversize.append(p.attachment.file_name)
            else:
                files.append((p.attachment.file_name, data))
                await self._add_jpeg_copy(files, p.attachment.file_name, data, limit)
        return files, oversize

    # ------------------------------------------------------------------ 新訊息
    async def handle(self, item: RelayItem) -> None:
        assert self.index is not None
        pieces = [p for p in item.pieces if (p.tg_chat, p.tg_msg) not in self.index]
        if not pieces:
            log.info("TG #%s 已搬過，跳過", item.first.tg_msg)
            return

        cfg, tz = self.cfg, self.cfg.tz
        limit = self.dc.filesize_limit(cfg.upload_limit_mb)

        # 附件分批：每批 ≤10 個且總大小 ≤ 上限；每批「發送前才下載、發完立刻釋放」，
        # 所以記憶體高峰只有一批（約 20MB），不會因為 10 張大圖的相簿吃掉 200MB。
        atts = [p.attachment for p in pieces if p.attachment]
        oversize = [a.file_name for a in atts if a.file_size and a.file_size > limit]
        groups = plan_groups([a for a in atts if not (a.file_size and a.file_size > limit)], limit)

        date_str = formatting.local_date(pieces[0].dt, tz)
        first_of_day = date_str != self.last_header_date
        body = "\n".join(p.text for p in pieces if p.text and p.text.strip())

        sent: list[discord.Message] = []
        for gi, group in enumerate(groups or [[]]):
            files: list[tuple[str, bytes]] = []
            late: list[str] = []
            for a in group:
                try:
                    data = await self._download(a, limit)
                except TelegramError as e:
                    log.warning("下載 %s 失敗：%s，改貼檔名", a.file_name, e)
                    data = None
                if data is None:
                    late.append(a.file_name)
                else:
                    files.append((a.file_name, data))
                    await self._add_jpeg_copy(files, a.file_name, data, limit)
            if gi == 0:
                text = formatting.build_mirror_content(
                    date_str, body, oversize + late, pieces[0].src_url, cfg.header_style, with_header=first_of_day)
            else:
                text = "\n".join(formatting.oversize_line(n) for n in late) or None
            try:
                msg = await self.dc.send(cfg.ch_inbox_mirror, text, files)
            except discord.HTTPException as e:
                if e.status != 413 or not files:
                    raise
                log.warning("Discord 回 413（檔案太大），改貼檔名：%s", ", ".join(n for n, _ in files))
                names = [n for n, _ in files]
                files.clear()
                if gi == 0:
                    text = formatting.build_mirror_content(
                        date_str, body, oversize + late + names, pieces[0].src_url, cfg.header_style,
                        with_header=first_of_day)
                else:
                    text = "\n".join(formatting.oversize_line(n) for n in late + names)
                msg = await self.dc.send(cfg.ch_inbox_mirror, text)
            files.clear()  # 立刻釋放記憶體
            sent.append(msg)
            oversize += late
        if first_of_day:
            self.last_header_date = date_str
        mirror = sent[0].jump_url
        log.info("已搬移 TG #%s%s → Discord %s（附件 %d，過大 %d%s）",
                 pieces[0].tg_msg, f"～#{pieces[-1].tg_msg}" if len(pieces) > 1 else "",
                 sent[0].id, sum(1 for p in pieces if p.attachment) - len(oversize), len(oversize),
                 "，含日期標頭" if first_of_day else "")

        keys = []
        for p in pieces:
            rec = records.KwRecord(
                tg_chat=p.tg_chat,
                tg_msg=p.tg_msg,
                origin="live",
                date=formatting.local_minute(p.dt, tz),
                type=p.type,
                mirror=mirror,
                src=p.src_url,
                filename=p.attachment.file_name if p.attachment else None,
                fid=p.attachment.file_unique_id if p.attachment else None,
                excerpt=records.make_excerpt(p.text) if p.text else None,
            )
            try:
                msg = await self.dc.send(cfg.ch_keywords, records.format_kw(rec))
                self.index.add(rec, msg)
                keys.append(rec.key)
                if p.text:
                    self.text_cache[rec.key] = p.text
            except Exception:
                log.exception("寫入 #關鍵字庫 失敗（TG #%s）", p.tg_msg)
        if keys:
            self.after_relay(keys, mirror)

    def after_relay(self, keys: list, mirror_url: str) -> None:
        """搬完 → 刪除同步探測（5 分鐘後）→ 分析；沒開刪除同步就直接分析。"""
        if getattr(self, "ocr", None) is not None:
            self.ocr.submit_entries([e for e in (self.index.get(k) for k in keys) if e is not None])
        if self.prober is not None:
            self.prober.schedule(keys, mirror_url)
        elif self.enricher is not None:
            self.enricher.submit(keys, mirror_url)

    # ------------------------------------------------------------------ 刪除同步
    async def on_alive(self, keys: list, mirror_url: str) -> None:
        if self.enricher is not None:
            self.enricher.submit(keys, mirror_url)

    async def on_deleted(self, keys: list, mirror_url: str, all_deleted: bool) -> None:
        assert self.index is not None
        dates = set()
        reply_urls = set()
        for k in keys:
            e = self.index.get(k)
            if e:
                dates.add(e.rec.date[:10])
                if e.rec.reply:
                    reply_urls.add(e.rec.reply)
                reply_urls.update((e.rec.shot or "").split())
            await self.index.delete(k)
            self.text_cache.pop(k, None)
        if all_deleted:
            mirror = await self.dc.fetch_by_url(mirror_url)
            if mirror is not None:
                had_header = formatting.parse_header_date(mirror.content)
                await mirror.delete()
                if had_header:
                    await self._repair_header(mirror, had_header)
            for u in reply_urls:
                await self.dc.delete_by_url(u)
            log.info("刪除同步：已刪除鏡像 %s（%d 則）", mirror_url.rsplit("/", 1)[-1], len(keys))
        else:
            log.info("刪除同步：相簿中 %d 則被刪，鏡像保留", len(keys))
        if self.dirty is not None:
            for d in dates:
                await self.dirty.mark(d)

    async def _repair_header(self, deleted, date: str, maybe: bool = False) -> None:
        """被刪的鏡像帶著當天的日期標頭 → 把標頭補到同一天的下一則；沒有下一則就讓下一則重新帶標頭。
        maybe=True：不確定被刪的那則有沒有標頭（沒快取到內容）→ 先確認當天前面還有沒有別的標頭。"""
        ch = getattr(deleted, "channel", None) or await self.dc.channel(self.cfg.ch_inbox_mirror)
        me = self.dc.client.user
        if maybe:
            async for prev in ch.history(limit=50, before=deleted):
                if me is not None and prev.author.id == me.id and formatting.parse_header_date(prev.content) == date:
                    return  # 當天前面還有標頭，不用補
                entries = self.index.by_mirror(prev.jump_url) if self.index else []
                if entries and entries[0].rec.date[:10] < date:
                    break   # 已經翻到前一天了
        async for nxt in ch.history(limit=20, after=deleted, oldest_first=True):
            if me is None or nxt.author.id != me.id or nxt.reference is not None:
                continue  # 跳過 AI 摘要回覆等
            entries = self.index.by_mirror(nxt.jump_url) if self.index else []
            if entries and entries[0].rec.date[:10] == date and not formatting.parse_header_date(nxt.content):
                head = formatting.header(date, self.cfg.header_style)
                await nxt.edit(content=formatting.truncate(f"{head}\n{nxt.content}".rstrip("\n"),
                                                           formatting.DISCORD_LIMIT))
            return
        if self.last_header_date == date:
            self.last_header_date = None

    # ------------------------------------------------------------------ 在 Discord 手動刪除
    async def on_discord_delete(self, channel_id: int, message_id: int, cached) -> None:
        """使用者直接在 Discord 刪了鏡像或 #關鍵字庫 紀錄。
        一般鏡像：Telegram 原訊息不受影響；反向同步（在 Discord 發的）：Telegram 那邊也刪。"""
        if self.index is None:
            return
        cfg = self.cfg
        if channel_id == cfg.ch_keywords:
            for k, e in list(self.index.entries.items()):
                if e.msg_id == message_id:
                    self.index.entries.pop(k, None)   # 紀錄被手動刪 → 程式也忘掉它
                    log.info("#關鍵字庫 紀錄被手動刪除：TG #%s", k[1])
            return
        if channel_id == getattr(cfg, "ch_extra", None):
            suffix = f"/{message_id}"
            for e in [e for e in self.index.entries.values() if e.rec.mirror.endswith(suffix)]:
                for u in [e.rec.reply] + (e.rec.shot or "").split():
                    if u:
                        await self.dc.delete_by_url(u)
                await self.index.delete(e.rec.key)
                log.info("#補充資料 %s 被手動刪除，紀錄一併刪除", e.rec.date[:10])
                if self.dirty is not None:
                    await self.dirty.mark(e.rec.date[:10])
            return
        if channel_id != cfg.ch_inbox_mirror:
            return
        suffix = f"/{message_id}"
        hits = [e for e in self.index.entries.values() if e.rec.mirror.endswith(suffix)]
        if not hits:
            return  # AI 摘要回覆、或 bot 自己刪的（刪除同步）
        dates = {e.rec.date[:10] for e in hits}
        if getattr(self, "reverse", None) is not None:
            await self.reverse.on_mirror_deleted(hits)   # 在 Discord 發的訊息被刪 → Telegram 也刪
        for e in hits:
            for u in [e.rec.reply] + (e.rec.shot or "").split():
                if u:
                    await self.dc.delete_by_url(u)
            await self.index.delete(e.rec.key)   # #關鍵字庫 紀錄整筆刪除
            self.text_cache.pop(e.rec.key, None)
        log.info("鏡像在 Discord 被刪除：一併刪除 %d 筆紀錄（%s）", len(hits), "、".join(sorted(dates)))
        for d in dates:
            had_header = formatting.parse_header_date(cached.content) if cached is not None else None
            await self._repair_header(discord.Object(id=message_id), d, maybe=had_header is None)
            if self.dirty is not None:
                await self.dirty.mark(d)

    # ------------------------------------------------------------------ 編輯同步
    async def handle_edit(self, piece: TgPiece) -> None:
        cfg = self.cfg
        assert self.index is not None
        if not cfg.edit_sync:
            return
        key = (piece.tg_chat, piece.tg_msg)
        e = self.index.get(key)
        if e is None or e.rec.origin != "live":
            return  # 還沒搬過、或是匯入的歷史訊息（多則合併，不做編輯同步）
        age_h = (datetime.now(timezone.utc) - piece.dt).total_seconds() / 3600
        if age_h > cfg.edit_sync_hours:
            log.info("TG #%s 的編輯超過 %.0f 小時，不同步", piece.tg_msg, cfg.edit_sync_hours)
            return
        mirror = await self.dc.fetch_by_url(e.rec.mirror)
        if mirror is None:
            return

        siblings = self.index.by_mirror(e.rec.mirror)
        self.text_cache[key] = piece.text
        body = "\n".join(
            (piece.text if s.rec.key == key else (self.text_cache.get(s.rec.key) or s.rec.excerpt or ""))
            for s in siblings)
        body = "\n".join(x for x in body.split("\n") if x.strip()) if body.strip() else ""
        header_date = formatting.parse_header_date(mirror.content)
        keep = [ln for ln in mirror.content.split("\n") if ln.startswith("📎 ")]
        content = formatting.build_mirror_content(
            header_date or e.rec.date[:10], body, None, piece.src_url, cfg.header_style,
            with_header=header_date is not None, extra_lines=keep)

        kwargs: dict = {"content": content or None}
        new_fid = piece.attachment.file_unique_id if piece.attachment else None
        if len(siblings) == 1 and piece.attachment and new_fid and new_fid != e.rec.fid:
            # 附件被換掉 → 重新上傳
            limit = self.dc.filesize_limit(cfg.upload_limit_mb)
            files, oversize = await self._collect_files([piece], limit)
            if files:
                kwargs["attachments"] = [discord.File(io.BytesIO(b), filename=n) for n, b in files]
            files.clear()
        await mirror.edit(**kwargs)
        for u in [e.rec.reply] + (e.rec.shot or "").split():
            if u:
                await self.dc.delete_by_url(u)
        await self.index.update(key, excerpt=records.make_excerpt(piece.text) if piece.text else None,
                                fid=new_fid or e.rec.fid, kw=None, gist=None, enriched=False, reply=None,
                                shot=None)
        log.info("編輯同步：TG #%s → 已更新鏡像 %s", piece.tg_msg, mirror.id)
        if self.dirty is not None:
            await self.dirty.mark(e.rec.date[:10])
        if self.enricher is not None:
            self.enricher.submit([s.rec.key for s in siblings], e.rec.mirror)
