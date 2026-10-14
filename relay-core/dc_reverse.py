# relay-core/dc_reverse.py
"""反向同步（第 7.12 節）：在 #投遞箱鏡像 直接發的訊息 → bot 轉發到 Telegram 投遞箱，編輯、刪除也同步。

- 使用者在 Discord 發的那則訊息「本身就是鏡像」：不另外貼一份，#關鍵字庫 紀錄 origin=dc、mirror 指向它。
- Telegram 那邊是 bot 發的訊息：bot 收不到自己發的訊息，所以不會再被搬回 Discord（不會無限循環）。
- 附件一批一批下載到記憶體、送出後立刻釋放；圖片用相片送（≤10MB），其他用檔案送；超過 50MB 只附檔名。
- 文字 ≤1024 字且有附件時當作第一個附件的說明；否則先送一則文字。
"""
from __future__ import annotations

import asyncio
import io
import logging

import discord
from telegram import InputMediaDocument, InputMediaPhoto
from telegram.error import BadRequest, TelegramError

import formatting
import records

log = logging.getLogger("reverse")

TG_UPLOAD_MAX = 50 * 1024 * 1024
TG_PHOTO_MAX = 10 * 1024 * 1024
CAPTION_MAX = 1024
TEXT_MAX = 4096
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp")


def is_photo(a) -> bool:
    ct = (a.content_type or "").lower()
    name = a.filename.lower()
    return (ct in ("image/jpeg", "image/png", "image/webp") or name.endswith(PHOTO_EXT)) and a.size <= TG_PHOTO_MAX


def split_text(text: str, n: int = TEXT_MAX) -> list[str]:
    out = []
    while text:
        out.append(text[:n])
        text = text[n:]
    return out


def file_id_of(msg) -> str | None:
    if msg.photo:
        return msg.photo[-1].file_unique_id
    for k in ("document", "video", "audio", "animation"):
        f = getattr(msg, k, None)
        if f is not None:
            return f.file_unique_id
    return None


def chunks(xs: list, n: int = 10) -> list[list]:
    return [xs[i:i + n] for i in range(0, len(xs), n)]


class ReverseSync:
    def __init__(self, relayer) -> None:
        self.r = relayer

    @property
    def cfg(self):
        return self.r.cfg

    def _mine(self, channel_id: int, author) -> bool:
        return (self.cfg.reverse_sync and channel_id == self.cfg.ch_inbox_mirror
                and author is not None and not author.bot and self.r.index is not None)

    # ------------------------------------------------------------------ 新訊息
    async def on_message(self, m: discord.Message) -> None:
        if not self._mine(m.channel.id, m.author) or m.webhook_id:
            return
        if m.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        await self._relay(m)

    async def _relay(self, m: discord.Message) -> int:
        """把一則 Discord 訊息（含轉發的內容與附件）送到 Telegram 並記錄。回傳送出幾則。"""
        atts = formatting.atts_of(m)
        text = formatting.full_text(m, label=False)
        if not text and not atts:
            return 0
        async with self.r.lock:
            sent = await self._send(text, atts)
            await self._record(m, sent)
        return len(sent)

    async def backfill_forwards(self) -> None:
        """一次性：#投遞箱鏡像 以前轉發進來的訊息（當時沒有同步）補送到 Telegram，由舊到新。
        已經有 origin=dc 紀錄的跳過；全部做完才寫旗標，中途停掉下次會接著做。"""
        from paths import data_dir
        flag = data_dir() / "reverse-forward-v1"
        if flag.exists() or not self.cfg.reverse_sync or self.r.index is None:
            return
        channel = await self.r.dc.channel(self.cfg.ch_inbox_mirror)
        n = 0
        async for m in channel.history(limit=None, oldest_first=True):   # type: ignore[attr-defined]
            if (not self._mine(m.channel.id, m.author) or m.webhook_id or not getattr(m, "message_snapshots", None)
                    or m.type not in (discord.MessageType.default, discord.MessageType.reply)):
                continue
            if any(e.rec.origin == "dc" for e in self.r.index.by_mirror(m.jump_url)):
                continue
            try:
                n += 1 if await self._relay(m) else 0
            except Exception:
                log.exception("補送轉發訊息失敗（Discord %s）", m.id)
                return                                       # 不寫旗標，下次啟動再接著做
            await asyncio.sleep(1.5)                          # Telegram 發送頻率限制
        flag.write_text("done")
        log.info("🔁 補送過往轉發的訊息到 Telegram：%d 則", n)

    async def _send(self, text: str, atts: list) -> list[tuple]:
        """送到 Telegram；回傳 [(tg Message, type, filename, 帶的文字)]。"""
        bot, chat = self.r.bot, self.cfg.tg_inbox_chat_id
        big = [a for a in atts if a.size > TG_UPLOAD_MAX]
        ok = [a for a in atts if a.size <= TG_UPLOAD_MAX]
        if big:
            text = "\n".join([text] + [f"📎 {a.filename}（檔案太大，未同步到 Telegram）" for a in big]).strip()
        out: list[tuple] = []
        caption = None
        if text and ok and len(text) <= CAPTION_MAX:
            caption = text
        elif text:
            for part in split_text(text):
                msg = await bot.send_message(chat, part, disable_web_page_preview=False)
                out.append((msg, "link" if "http" in part else "text", None, part))
        photos = [a for a in ok if is_photo(a)]
        docs = [a for a in ok if not is_photo(a)]
        for kind, group_list in (("image", chunks(photos)), ("file", chunks(docs))):
            for group in group_list:
                data = [await a.read() for a in group]          # 一批（≤10 個）下載到記憶體
                cap, caption = caption, None                      # 說明只放在第一批的第一個
                try:
                    if len(group) == 1:
                        a, b = group[0], data[0]
                        if kind == "image":
                            msgs = [await bot.send_photo(chat, b, caption=cap, filename=a.filename)]
                        else:
                            msgs = [await bot.send_document(chat, b, caption=cap, filename=a.filename)]
                    else:
                        media = [(InputMediaPhoto(b, caption=cap if i == 0 else None, filename=a.filename)
                                  if kind == "image" else
                                  InputMediaDocument(b, caption=cap if i == 0 else None, filename=a.filename))
                                 for i, (a, b) in enumerate(zip(group, data))]
                        msgs = list(await bot.send_media_group(chat, media))
                finally:
                    data.clear()                                  # 立刻釋放
                for i, (a, msg) in enumerate(zip(group, msgs)):
                    out.append((msg, kind, a.filename, cap if i == 0 else None))
        return out

    async def _record(self, m: discord.Message, sent: list[tuple]) -> None:
        r, cfg = self.r, self.cfg
        keys = []
        for msg, kind, filename, carried in sent:
            rec = records.KwRecord(
                tg_chat=msg.chat_id, tg_msg=msg.message_id, origin="dc",
                date=formatting.local_minute(m.created_at, cfg.tz), type=kind, mirror=m.jump_url,
                filename=filename, fid=file_id_of(msg),
                excerpt=records.make_excerpt(carried) if carried else None)
            try:
                kmsg = await r.dc.send(cfg.ch_keywords, records.format_kw(rec))
                r.index.add(rec, kmsg)
                keys.append(rec.key)
                if carried:
                    r.text_cache[rec.key] = carried
            except Exception:
                log.exception("寫入 #關鍵字庫 失敗（反向同步 TG #%s）", msg.message_id)
        log.info("反向同步：Discord %s → Telegram %d 則", m.id, len(sent))
        if keys:
            r.after_relay(keys, m.jump_url)

    # ------------------------------------------------------------------ 編輯
    async def on_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        m = payload.message
        if not self._mine(payload.channel_id, getattr(m, "author", None)) or not self.cfg.edit_sync:
            return
        r = self.r
        async with r.lock:
            hits = sorted((e for e in r.index.by_mirror(m.jump_url) if e.rec.origin == "dc"),
                          key=lambda e: e.rec.tg_msg)
            if not hits:
                return
            bot = r.bot
            changed = False
            # 1) 在 Discord 拿掉的附件 → Telegram 那則也刪掉
            names = {a.filename for a in formatting.atts_of(m)}
            for e in list(hits):
                if e.rec.filename and e.rec.filename not in names:
                    await self._tg_delete(e.rec.tg_chat, e.rec.tg_msg)
                    await r.index.delete(e.rec.key)
                    hits.remove(e)
                    changed = True
            # 2) 文字改了 → 改 Telegram 的文字／說明
            new = formatting.full_text(m, label=False)
            if hits:
                first = hits[0]
                old = r.text_cache.get(first.rec.key)
                if old is None:
                    old = first.rec.excerpt or ""
                    same = records.make_excerpt(new) == old if new else not old
                else:
                    same = new == old
                if not same:
                    await self._edit_text(first, new)
                    changed = True
            if not changed:
                return                                   # Discord 只是補上連結預覽之類的，不用處理
            for e in hits:
                for u in [e.rec.reply] + (e.rec.shot or "").split():
                    if u:
                        await r.dc.delete_by_url(u)
                await r.index.update(e.rec.key, kw=None, gist=None, enriched=False, reply=None, shot=None)
            log.info("反向同步：Discord %s 的編輯已同步到 Telegram", m.id)
            if r.dirty is not None and hits:
                await r.dirty.mark(hits[0].rec.date[:10])
            if r.enricher is not None and hits:
                r.enricher.submit([e.rec.key for e in hits], m.jump_url)

    async def _edit_text(self, e, new: str) -> None:
        r, bot = self.r, self.r.bot
        chat, mid = e.rec.tg_chat, e.rec.tg_msg
        try:
            if e.rec.type in ("text", "link"):
                if new:
                    await bot.edit_message_text(new[:TEXT_MAX], chat_id=chat, message_id=mid)
                else:
                    await self._tg_delete(chat, mid)       # 文字清空了 → 那則文字訊息刪掉
                    await r.index.delete(e.rec.key)
                    r.text_cache.pop(e.rec.key, None)
                    return
            else:
                cap = new if len(new) <= CAPTION_MAX else new[:CAPTION_MAX - 1] + "…"
                await bot.edit_message_caption(chat_id=chat, message_id=mid, caption=cap or None)
        except BadRequest as ex:
            if "not modified" not in str(ex).lower():
                log.warning("Telegram 編輯失敗（#%s）：%s", mid, ex)
        r.text_cache[e.rec.key] = new
        await r.index.update(e.rec.key, excerpt=records.make_excerpt(new) if new else None)

    # ------------------------------------------------------------------ 刪除
    async def _tg_delete(self, chat: int, mid: int) -> None:
        try:
            await self.r.bot.delete_message(chat, mid)
        except TelegramError as ex:
            log.warning("Telegram 刪除失敗（#%s）：%s", mid, ex)

    async def on_mirror_deleted(self, hits: list) -> None:
        """relay.on_discord_delete 呼叫：origin=dc 的紀錄 → Telegram 那幾則也刪掉。"""
        if not self.cfg.reverse_sync:
            return
        for e in hits:
            if e.rec.origin == "dc":
                await self._tg_delete(e.rec.tg_chat, e.rec.tg_msg)
