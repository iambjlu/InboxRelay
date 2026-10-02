# relay-core/tg_intake.py
"""Telegram 實時收訊：群組類型偵測、migrate 處理、訊息分類、相簿緩衝。

收到的訊息會整理成 RelayItem 放進佇列，由 relay.Relayer 逐一發到 Discord。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from telegram import Chat, Message, MessageEntity, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import formatting
from album import AlbumBuffer

log = logging.getLogger("tg_intake")


@dataclass
class AttachmentRef:
    file_id: str
    file_name: str
    file_size: int | None
    file_unique_id: str | None = None


@dataclass
class TgPiece:
    """一則 Telegram 訊息。"""
    tg_chat: int
    tg_msg: int
    dt: datetime
    text: str
    type: str
    attachment: AttachmentRef | None = None
    src_url: str | None = None


@dataclass
class RelayItem:
    """要變成一則 Discord 鏡像訊息的單位：單則訊息，或一整本相簿。
    edit=True 代表這是「Telegram 上被編輯過的訊息」（編輯同步）。"""
    pieces: list[TgPiece] = field(default_factory=list)
    edit: bool = False

    @property
    def first(self) -> TgPiece:
        return self.pieces[0]


_URL_ENTITIES = {MessageEntity.URL, MessageEntity.TEXT_LINK}


def classify(msg: Message) -> tuple[str, AttachmentRef | None, str] | None:
    """回傳 (type, 附件, 文字)；服務訊息（加人、改名、置頂…）回傳 None。"""
    text = msg.text or msg.caption or ""
    mid = msg.message_id

    # 注意：animation 訊息同時也帶 document，要先判斷
    if msg.photo:
        p = msg.photo[-1]
        return "image", AttachmentRef(p.file_id, f"photo_{mid}.jpg", p.file_size, p.file_unique_id), text
    if msg.animation:
        a = msg.animation
        return "video", AttachmentRef(a.file_id, a.file_name or f"animation_{mid}.mp4", a.file_size, a.file_unique_id), text
    if msg.video:
        v = msg.video
        return "video", AttachmentRef(v.file_id, v.file_name or f"video_{mid}.mp4", v.file_size, v.file_unique_id), text
    if msg.video_note:
        v = msg.video_note
        return "video", AttachmentRef(v.file_id, f"video_note_{mid}.mp4", v.file_size, v.file_unique_id), text
    if msg.voice:
        v = msg.voice
        return "voice", AttachmentRef(v.file_id, f"voice_{mid}.ogg", v.file_size, v.file_unique_id), text
    if msg.audio:
        a = msg.audio
        return "file", AttachmentRef(a.file_id, a.file_name or f"audio_{mid}.mp3", a.file_size, a.file_unique_id), text
    if msg.document:
        d = msg.document
        kind = "image" if (d.mime_type or "").startswith("image/") else "file"
        return kind, AttachmentRef(d.file_id, d.file_name or f"file_{mid}", d.file_size, d.file_unique_id), text
    if msg.sticker:
        return "sticker", None, text or f"[貼圖 {msg.sticker.emoji or ''}]".replace(" ]", "]")
    if msg.location:
        loc = msg.location
        return "text", None, text or f"📍 位置 {loc.latitude:.5f}, {loc.longitude:.5f}"
    if msg.contact:
        c = msg.contact
        return "text", None, text or f"👤 聯絡人 {c.first_name} {c.last_name or ''}".strip()
    if msg.poll:
        return "text", None, text or f"📊 投票：{msg.poll.question}"
    if text.strip():
        ents = (msg.entities or ()) + (msg.caption_entities or ())
        kind = "link" if any(e.type in _URL_ENTITIES for e in ents) else "text"
        return kind, None, text
    return None


class TelegramIntake:
    def __init__(self, token: str, chat_id: int, tz: str, src_link: bool,
                 queue: "asyncio.Queue[RelayItem]") -> None:
        self.chat_id = chat_id
        self.tz = tz
        self.src_link_wanted = src_link
        self.queue = queue
        self.chat_type: str | None = None
        self.chat_title: str | None = None
        self.app = (
            Application.builder().token(token)
            .read_timeout(60).write_timeout(60).connect_timeout(20)
            .build()
        )
        # PTB 預設逐一處理 update，因此 Discord 端順序會和 Telegram 一致
        self.app.add_handler(MessageHandler(filters.ALL, self._handle))
        self.albums: AlbumBuffer[TgPiece] = AlbumBuffer(
            self._emit, delay=2.0, sort_key=lambda p: p.tg_msg)

    @property
    def bot(self):
        return self.app.bot

    @property
    def src_enabled(self) -> bool:
        """一般群組沒有訊息連結，自動停用 src。"""
        return self.src_link_wanted and self.chat_type == Chat.SUPERGROUP

    async def initialize(self) -> None:
        await self.app.initialize()
        chat = await self.app.bot.get_chat(self.chat_id)
        self.chat_type, self.chat_title = chat.type, chat.title
        log.info("投遞箱：%s（%s），src 連結：%s",
                 self.chat_title, self.chat_type, "開" if self.src_enabled else "關")

    async def start_polling(self) -> None:
        await self.app.start()
        await self.app.updater.start_polling(allowed_updates=["message", "edited_message"])
        log.info("開始輪詢 Telegram")

    async def stop(self) -> None:
        if self.app.updater and self.app.updater.running:
            await self.app.updater.stop()
        await self.albums.flush_all()
        if self.app.running:
            await self.app.stop()
        await self.app.shutdown()

    async def _emit(self, pieces: list[TgPiece]) -> None:
        if len(pieces) > 1:
            for p in pieces:
                p.type = "album"
        await self.queue.put(RelayItem(pieces))

    async def _handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.edited_message is not None:
            await self._handle_edit(update.edited_message)
            return
        msg = update.message
        if msg is None:
            return

        # 群組升級成超級群組：舊群組會收到 migrate_to_chat_id
        if msg.migrate_to_chat_id and msg.chat_id == self.chat_id:
            old, self.chat_id = self.chat_id, msg.migrate_to_chat_id
            self.chat_type = Chat.SUPERGROUP
            log.warning("投遞箱已升級為超級群組：%s → %s（請更新 .env 的 TG_INBOX_CHAT_ID）",
                        old, self.chat_id)
            return

        if msg.chat_id != self.chat_id:
            return

        c = classify(msg)
        if c is None:
            log.info("略過服務訊息 #%s", msg.message_id)
            return
        kind, att, text = c
        src = formatting.supergroup_src_url(msg.chat_id, msg.message_id) if self.src_enabled else None
        piece = TgPiece(msg.chat_id, msg.message_id, msg.date, text, kind, att, src)
        await self.albums.add(msg.media_group_id, piece)

    async def _handle_edit(self, msg: Message) -> None:
        if msg.chat_id != self.chat_id:
            return
        c = classify(msg)
        if c is None:
            return
        kind, att, text = c
        src = formatting.supergroup_src_url(msg.chat_id, msg.message_id) if self.src_enabled else None
        piece = TgPiece(msg.chat_id, msg.message_id, msg.date, text, kind, att, src)
        await self.albums.flush_all()  # 先讓前面的訊息搬完，順序才不會亂
        await self.queue.put(RelayItem([piece], edit=True))
