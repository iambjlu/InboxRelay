# relay-core/enrich/shotbutton.py
"""「📸 無法截圖」通知上的「重試截圖」按鈕（持久按鈕：custom_id 固定，App 重開後仍然有效）。

按鈕本身不帶資料：按下去時從通知訊息的內容讀出網址、日期和鏡像連結（parse_notice），
交給 ShotWorker.retry_notice 重拍。通知訊息格式見 enrich/screenshot.post_one。
"""
from __future__ import annotations

import re

import discord

CUSTOM_ID = "inboxrelay:shot_retry"
_NOTICE = re.compile(r"^📸 無法截圖｜(?P<when>[^｜\n]*)｜<(?P<url>[^>\n]+)>", re.M)
_BACK = re.compile(r"\[↗ [^\]]*\]\(<(?P<back>[^>]+)>\)")


def parse_notice(content: str) -> tuple[str, str, str] | None:
    """通知內容 → (日期, 網址, 鏡像連結)；不是失敗通知回傳 None。"""
    m = _NOTICE.search(content or "")
    b = _BACK.search(content or "")
    if not m or not b:
        return None
    return m.group("when"), m.group("url"), b.group("back")


class RetryView(discord.ui.View):
    """handler=None 的用來附在新發的通知上（只負責顯示按鈕）；有 handler 的那份由 client.add_view 註冊，負責處理點擊。"""

    def __init__(self, handler=None) -> None:
        super().__init__(timeout=None)
        self.handler = handler

    @discord.ui.button(label="重試截圖", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id=CUSTOM_ID)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = "還沒準備好，請稍後再按"
        if self.handler is not None and interaction.message is not None:
            try:
                text = await self.handler(interaction.message)
            except Exception as e:                      # 不讓使用者看到「互動失敗」
                text = f"⚠️ 重試失敗：{type(e).__name__}: {str(e)[:150]}"
        await interaction.followup.send(text, ephemeral=True)
