# relay-core/enrich/audiobutton.py
"""音檔摘要訊息上的「詳細整理」按鈕（持久按鈕）。

按下去：讀摘要訊息附件裡的逐字稿 → 產生「詳細重點＋總結」→ 貼回 #投遞箱鏡像（bot 發的訊息）並同步發到 Telegram，
這樣兩邊都看得到，也會像一般訊息一樣存進紀錄（刪除同步、搜尋、總結都認得）。
"""
from __future__ import annotations

import logging
import re

import discord

import formatting
from enrich import audio
from recap import daily

log = logging.getLogger("audiobutton")

CUSTOM_ID = "inboxrelay:audio_notes"
_BACK = re.compile(r"\[↗ [^\]]*\]\(<(?P<back>[^>]+)>\)")


class NotesView(discord.ui.View):
    """handler=None＝附在新訊息上只負責顯示；有 handler 的那份由 client.add_view 註冊負責處理點擊。"""

    def __init__(self, handler=None) -> None:
        super().__init__(timeout=None)
        self.handler = handler

    @discord.ui.button(label="詳細整理", emoji="📝", style=discord.ButtonStyle.primary, custom_id=CUSTOM_ID)
    async def notes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = "還沒準備好，請稍後再按"
        if self.handler is not None and interaction.message is not None:
            try:
                text = await self.handler(interaction.message)
            except Exception as e:
                log.exception("音檔詳細整理失敗")
                text = f"⚠️ 整理失敗：{type(e).__name__}: {str(e)[:150]}"
        await interaction.followup.send(text, ephemeral=True)


class AudioNotes:
    def __init__(self, relayer, llm, dc, cfg) -> None:
        self.r, self.llm, self.dc, self.cfg = relayer, llm, dc, cfg
        self._busy: set[int] = set()

    def mirror_url_of(self, msg: discord.Message) -> str | None:
        m = _BACK.search(msg.content or "")
        if m:
            return m.group("back")
        ref = getattr(msg, "reference", None)                      # 沒搬到預覽頻道的摘要＝回覆在鏡像下面
        return ref.jump_url if ref is not None and getattr(ref, "jump_url", None) else None

    async def on_click(self, msg: discord.Message) -> str:
        if msg.id in self._busy:
            return "正在整理中，完成後會貼到鏡像"
        files = [a for a in msg.attachments if a.filename.lower().endswith(".txt")]
        if not files:
            return "找不到這段錄音的逐字稿（附件 .txt 被刪掉了？）"
        mirror_url = self.mirror_url_of(msg)
        mirror = await self.dc.fetch_by_url(mirror_url) if mirror_url else None
        channel = mirror.channel if mirror is not None else await self.dc.channel(self.cfg.ch_inbox_mirror)
        self._busy.add(msg.id)
        try:
            posted = 0
            for a in files:
                name = a.filename[:-4]
                transcript = (await a.read()).decode("utf-8", errors="replace")
                notes = await audio.detailed_notes(self.llm, name, transcript)
                if not notes.strip():
                    continue
                await self._publish(channel, f"🎙️ **{name}｜詳細整理**\n{notes}")
                posted += 1
            return f"✅ 已貼到鏡像並傳到 Telegram（{posted} 份）" if posted else "模型沒有產生內容，請稍後再試"
        finally:
            self._busy.discard(msg.id)

    async def _publish(self, channel, text: str) -> None:
        """貼到 #投遞箱鏡像（可能分多則）並發到 Telegram，第一則 Discord 訊息當這幾筆紀錄的鏡像。"""
        from dc_reverse import ReverseSync
        chunks = daily.split_message(text, limit=1990)
        first = None
        for c in chunks:
            m = await channel.send(c, suppress_embeds=True)
            first = first or m
        rs = self.r.reverse or ReverseSync(self.r)
        async with self.r.lock:
            sent = await rs._send(text.replace("**", ""), [])      # Telegram 是純文字，不留星號
            await rs._record(first, sent)
