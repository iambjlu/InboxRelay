# relay-core/dc_output.py
"""Discord 發文端。只用 guilds intent，不需要 Message Content。"""
from __future__ import annotations

import asyncio
import io
import logging

import discord

import formatting
import records

log = logging.getLogger("dc_output")

TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API getFile 上限


class DiscordOut:
    def __init__(self, token: str, guild_id: int, read_messages: bool = False) -> None:
        self._token = token
        self.guild_id = guild_id
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True   # 收「訊息被刪除」事件（非特權）
        if read_messages:
            # 讀使用者訊息內容（AI 測試、搜尋、通知頻道）；要先在 Developer Portal 開 Message Content Intent
            intents.message_content = True
        self.client = discord.Client(
            intents=intents,
            max_messages=100,       # discord.py 預設快取 1000 則訊息；我們用不到，壓小省記憶體
            allowed_mentions=discord.AllowedMentions.none(),  # 搬運內容不准 @ 任何人
        )
        self._task: asyncio.Task | None = None
        self._handlers: list = []
        self._delete_handlers: list = []
        self._edit_handlers: list = []

        @self.client.event
        async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent) -> None:
            for h in list(self._edit_handlers):
                try:
                    await h(payload)
                except Exception:
                    log.exception("處理編輯事件失敗")

        @self.client.event
        async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent) -> None:
            for h in list(self._delete_handlers):
                try:
                    await h(payload.channel_id, payload.message_id, payload.cached_message)
                except Exception:
                    log.exception("處理刪除事件失敗")

        @self.client.event
        async def on_message(message: discord.Message) -> None:
            for h in list(self._handlers):
                try:
                    await h(message)
                except Exception:
                    log.exception("處理訊息失敗")

    def add_delete_handler(self, handler) -> None:
        """註冊「訊息被刪除」處理函式：handler(channel_id, message_id, cached_message_or_None)。"""
        self._delete_handlers.append(handler)

    def add_edit_handler(self, handler) -> None:
        """註冊「訊息被編輯」處理函式：handler(RawMessageUpdateEvent)。"""
        self._edit_handlers.append(handler)

    def add_message_handler(self, handler) -> None:
        """註冊 on_message 處理函式（AI 測試頻道、搜尋頻道、通知頻道…各自判斷是不是自己的頻道）。"""
        self._handlers.append(handler)

    async def start(self) -> None:
        await self.client.login(self._token)
        self._task = asyncio.create_task(self.client.connect(reconnect=True))
        ready = asyncio.create_task(self.client.wait_until_ready())
        done, _ = await asyncio.wait({ready, self._task}, timeout=60, return_when=asyncio.FIRST_COMPLETED)
        if self._task in done:  # 連線就失敗了（例如沒開 Privileged Intent）
            ready.cancel()
            exc = self._task.exception()
            raise exc if exc else RuntimeError("Discord 連線意外結束")
        if ready not in done:
            ready.cancel()
            raise TimeoutError("Discord 60 秒內沒有就緒")
        log.info("Discord 已連線：%s", self.client.user)

    @property
    def guild(self) -> discord.Guild | None:
        return self.client.get_guild(self.guild_id)

    def filesize_limit(self, configured_mb: float = 20) -> int:
        """實際上限 = min(Telegram Bot API 下載上限 20MB, Discord 上傳上限)。

        discord.py 的 guild.filesize_limit 對未加成伺服器仍回報舊的 10MB，
        但 Discord 已在 2026 年把免費上限調成 20MB，所以取兩者較大值，
        並允許用 .env 的 DC_UPLOAD_LIMIT_MB 覆寫。真的超過時 Discord 會回 413，
        relay 會自動改貼檔名。
        """
        g = self.guild
        guild_limit = g.filesize_limit if g else 0
        discord_limit = max(guild_limit, int(configured_mb * 1024 * 1024))
        return min(TG_DOWNLOAD_LIMIT, discord_limit)

    async def channel(self, channel_id: int) -> discord.abc.Messageable:
        ch = self.client.get_channel(channel_id)
        if ch is None:
            ch = await self.client.fetch_channel(channel_id)
        return ch  # type: ignore[return-value]

    async def check_channels(self, channels: dict[str, int]) -> list[str]:
        """回傳問題清單；空清單代表全部頻道都可讀可寫。"""
        problems: list[str] = []
        g = self.guild
        if g is None:
            return [f"Bot 不在伺服器 {self.guild_id} 裡"]
        for name, cid in channels.items():
            try:
                ch = await self.channel(cid)
            except discord.DiscordException as e:
                problems.append(f"#{name}（{cid}）讀不到：{e}")
                continue
            perms = ch.permissions_for(g.me)  # type: ignore[union-attr]
            lacking = [
                p
                for p in ("view_channel", "send_messages", "read_message_history",
                          "attach_files", "embed_links", "manage_messages")
                if not getattr(perms, p)
            ]
            if lacking:
                problems.append(f"#{name} 缺權限：{', '.join(lacking)}")
        return problems

    async def send(self, channel_id: int, content: str | None,
                   files: list[tuple[str, bytes]] | None = None, retries: int = 3) -> discord.Message:
        """發一則訊息；附件以 (檔名, bytes) 傳入，每次重試都重新包成 discord.File。"""
        ch = await self.channel(channel_id)
        delay = 2.0
        for attempt in range(1, retries + 1):
            dfiles = [discord.File(io.BytesIO(b), filename=n) for n, b in (files or [])]
            try:
                kwargs = {}
                if content:
                    kwargs["content"] = content
                if dfiles:
                    kwargs["files"] = dfiles
                return await ch.send(**kwargs)
            except discord.Forbidden:
                raise  # 權限問題重試也沒用，避免累積無效請求
            except (discord.HTTPException, OSError) as e:
                if attempt == retries:
                    raise
                log.warning("Discord 發文失敗（第 %d 次）：%s，%.0f 秒後重試", attempt, e, delay)
                await asyncio.sleep(delay)
                delay *= 2
            finally:
                for f in dfiles:
                    f.close()
        raise RuntimeError("unreachable")

    async def send_file(self, channel_id: int, content: str, filename: str, data: bytes) -> discord.Message:
        """發一則附單一檔案的訊息，並關掉 Discord 自動連結預覽（截圖頻道用）。"""
        ch = await self.channel(channel_id)
        return await ch.send(content, file=discord.File(io.BytesIO(data), filename=filename),
                             suppress_embeds=True)

    async def send_chunks(self, channel_id: int, content: str | None,
                          files: list[tuple[str, bytes]], limit: int) -> list[discord.Message]:
        """附件超過 10 個或總大小超過上限時拆成多則；文字只放在第一則。"""
        groups: list[list[tuple[str, bytes]]] = []
        cur: list[tuple[str, bytes]] = []
        size = 0
        for name, data in files:
            if cur and (len(cur) == 10 or size + len(data) > limit):
                groups.append(cur)
                cur, size = [], 0
            cur.append((name, data))
            size += len(data)
        if cur:
            groups.append(cur)
        if not groups:
            if not content:
                raise ValueError("沒有內容也沒有附件")
            return [await self.send(channel_id, content)]
        sent = []
        for i, g in enumerate(groups):
            sent.append(await self.send(channel_id, content if i == 0 else None, g))
        return sent

    async def fetch_by_url(self, url: str) -> discord.Message | None:
        ids = records.parse_message_url(url)
        if not ids:
            return None
        ch = await self.channel(ids[0])
        try:
            return await ch.fetch_message(ids[1])  # type: ignore[attr-defined]
        except discord.NotFound:
            return None

    async def delete_by_url(self, url: str) -> None:
        ids = records.parse_message_url(url)
        if not ids:
            return
        ch = await self.channel(ids[0])
        try:
            await ch.get_partial_message(ids[1]).delete()  # type: ignore[attr-defined]
        except discord.NotFound:
            pass

    async def load_kw_keys(self, channel_id: int) -> set[tuple[int, int]]:
        """讀取 #關鍵字庫 全部紀錄的 (tg_chat, tg_msg)，作為去重集合。"""
        ch = await self.channel(channel_id)
        me = self.client.user
        keys: set[tuple[int, int]] = set()
        async for m in ch.history(limit=None):  # type: ignore[attr-defined]
            if me is not None and m.author.id == me.id:
                k = records.parse_key(m.content)
                if k:
                    keys.add(k)
        return keys

    async def last_header_date(self, channel_id: int, scan: int = 100) -> str | None:
        """從頻道最近的訊息往回找，取最後一個由本 bot 發出的日期標頭。

        用途：重啟後判斷下一則是否為「當天第一則」，避免同一天重複出現大日期。
        """
        ch = await self.channel(channel_id)
        me = self.client.user
        async for m in ch.history(limit=scan):  # type: ignore[attr-defined]
            if me is None or m.author.id != me.id:
                continue
            d = formatting.parse_header_date(m.content)
            if d:
                return d
        return None

    async def close(self) -> None:
        await self.client.close()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
