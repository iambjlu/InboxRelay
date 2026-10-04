# relay-core/aitest.py
"""AI 測試頻道：把東西丟進去，看 AI 怎麼摘要與總結。

- 一般訊息：自動分析（連結摘要、圖片摘要、檔案總結、關鍵字），回覆在該訊息下。
- `!總結`：把頻道「今天」的內容做成每日總結預覽（同 M5 的 prompt）。
  `!總結 2026-10-02` 指定日期；`!總結 全部` 最近 200 則。
- `!說明`：顯示用法。
需要在 Developer Portal 開啟 Message Content Intent。
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field

import discord

import formatting
import activity
from config import Config
from enrich.document import LEGACY_EXT, ext_of, is_supported, summarize_document
from enrich.image import MIME, summarize_image
from enrich.keywords import extract_keywords
from enrich.link import find_urls, summarize_link
from llm import LLM
from memory import MemoryStore
from recap import daily

log = logging.getLogger("aitest")

MAX_FILE = 20 * 1024 * 1024
HELP = (
    "🧪 **AI 測試頻道用法**\n"
    "• 直接丟文字、連結、圖片、檔案 → 我會回覆摘要和關鍵字\n"
    "• `!總結` → 今天這個頻道的每日總結預覽\n"
    "• `!總結 2026-10-02` → 指定日期　• `!總結 全部` → 最近 200 則\n"
    "• `更正：8888 是拜拜` → 教我你的用語，記憶會馬上升級\n"
    "• `!記憶` → 看目前的記憶　• `!記憶 升級` → 立刻整理一次\n"
    "-# 檔案只讀不執行、全程在記憶體處理"
)


@dataclass
class Enriched:
    time: str
    kind: str
    text: str
    parts: list[tuple[str, str]] = field(default_factory=list)  # (標題, 摘要)
    kw: list[str] = field(default_factory=list)
    gist: str = ""
    url: str = ""

    def material(self) -> str:
        bits = [self.text] if self.text else []
        bits += [f"{t}：{s}" for t, s in self.parts]
        return "；".join(bits)


class AiTestChannel:
    def __init__(self, cfg: Config, dc, llm: LLM) -> None:
        self.cfg = cfg
        self.dc = dc
        self.client = dc.client
        self.llm = llm
        self.memory: MemoryStore | None = None  # 由 main 在連線後設定
        self.prompts = None                      # memory.PromptStore
        self.cache: "OrderedDict[int, Enriched]" = OrderedDict()   # 只留最近 300 則的分析結果
        dc.add_message_handler(self._on_message)

    async def _on_message(self, m: discord.Message) -> None:
        # 使用者在 #資料庫 手動發新的 #記憶／#提示詞 → 重新載入
        if self.memory and self.memory.on_user_override(m):
            await self.memory.load()
            log.info("偵測到手動 #記憶，已載入 v%d", self.memory.version)
            return
        if self.prompts and self.prompts.is_user_edit(m):
            warnings = await self.prompts.load(publish_defaults=True)
            log.info("偵測到手動 #提示詞，已重新載入%s", "（有警告）" if warnings else "")
            return
        if m.channel.id != self.cfg.ch_ai_test or m.author.bot:
            return
        c = m.content.strip()
        if c in ("!說明", "!help"):
            await m.reply(HELP, mention_author=False)
            return
        if c.startswith("!記憶"):
            asyncio.create_task(self._safe(self._memory_cmd(m, c[len("!記憶"):].strip()), m))
            return
        if c.startswith(("更正：", "更正:")) and self.memory:
            self.memory.add_correction(re.split(r"[：:]", c, maxsplit=1)[1])
            await m.reply("📝 收到更正，記憶升級中…", mention_author=False)
            asyncio.create_task(self._safe(self.memory.upgrade("使用者更正"), m))
            return
        if c.startswith("!總結"):
            asyncio.create_task(self._safe(self._recap(m, c[len("!總結"):].strip()), m))
            return
        if not c and not m.attachments:
            return
        asyncio.create_task(self._safe(self._analyze(m), m))

    async def _safe(self, coro, m: discord.Message) -> None:
        task = activity.start(f"AI 測試頻道：{(m.content.strip() or '附件')[:20]}", background=False)
        try:
            await coro
        except Exception as e:
            log.exception("AI 測試頻道處理失敗")
            try:
                await m.reply(f"⚠️ 處理失敗：{type(e).__name__}: {str(e)[:300]}", mention_author=False)
            except discord.HTTPException:
                pass
        finally:
            activity.finish(task)

    # ---------------------------------------------------------------- 單則分析
    async def enrich(self, m: discord.Message) -> Enriched:
        if m.id in self.cache:
            return self.cache[m.id]
        local = formatting.local_dt(m.created_at, self.cfg.tz)
        text = m.content.strip()
        e = Enriched(local.strftime("%H:%M"), "文字", text, url=m.jump_url)
        urls = find_urls(text)
        if urls:
            e.kind = "連結"
        for u in urls:
            e.parts.append((f"🔗 {u}", await summarize_link(self.llm, u)))
        for a in m.attachments:
            name = a.filename
            if a.size > MAX_FILE:
                e.parts.append((f"📎 {name}", "過大（> 20MB），略過"))
                continue
            is_img = (a.content_type or "").startswith("image/") or ext_of(name) in MIME
            if is_img:
                e.kind = "圖片"
                data = await a.read()
                s = await summarize_image(self.llm, data, name, text)
                e.parts.append((f"🖼️ {name}", s))
            elif is_supported(name) or ext_of(name) in LEGACY_EXT:
                e.kind = "檔案"
                data = await a.read()
                s = await summarize_document(self.llm, name, data)
                e.parts.append((f"📄 {name}", s))
            else:
                e.kind = "檔案"
                e.parts.append((f"📎 {name}", "（不支援的類型，只記錄檔名）"))
            data = b""  # 立刻釋放
        if e.material():
            context = await self._context(m)
            e.kw, e.gist = await extract_keywords(self.llm, e.material(), context)
            if self.memory and self.memory.observe(e.material(), e.kw, e.gist):
                asyncio.create_task(self.memory.upgrade(f"自動：累積 {len(self.memory.observations)} 則新分析"))
        self.cache[m.id] = e
        while len(self.cache) > 300:
            self.cache.popitem(last=False)
        return e

    async def _context(self, m: discord.Message, n: int = 3) -> list[str]:
        """前幾則訊息當前後文，幫助理解「8888」這種短訊息。"""
        out: list[str] = []
        async for h in m.channel.history(limit=10, before=m):
            if h.author.bot or h.content.strip().startswith("!") or not h.content.strip():
                continue
            out.append(h.content.strip())
            if len(out) >= n:
                break
        return list(reversed(out))

    async def _memory_cmd(self, m: discord.Message, arg: str) -> None:
        if not self.memory:
            await m.reply("記憶功能未啟用", mention_author=False)
            return
        if arg == "升級":
            await m.reply("🧠 整理中…", mention_author=False)
            if not await self.memory.upgrade("手動"):
                await m.reply("目前沒有需要更新的內容（先丟幾則訊息或給我更正）", mention_author=False)
            return
        body = self.memory.content or "（尚無記憶）"
        for i, chunk in enumerate(daily.split_message(f"🧠 **目前記憶 v{self.memory.version}**\n{body}")):
            await (m.reply(chunk, mention_author=False) if i == 0 else m.channel.send(chunk))

    async def _analyze(self, m: discord.Message) -> None:
        async with m.channel.typing():
            e = await self.enrich(m)
            shots: list[tuple[str, bytes]] = []
            shot_fail: list[tuple[str, str]] = []
            if self.cfg.link_screenshot:
                from enrich.screenshot import capture_many
                shots = await capture_many(find_urls(m.content), failures=shot_fail)   # 實驗頻道的截圖直接貼在這裡
        lines = ["🤖 **AI 分析**"]
        for title, s in e.parts:
            lines.append(f"**{title}**")
            lines += [f"> {x}" for x in s.split("\n") if x.strip()]
        if e.kw:
            lines.append(f"🏷️ 關鍵字：{'、'.join(e.kw)}")
        if e.gist:
            lines.append(f"💡 重點：{e.gist}")
        if shots:
            lines.append("📸 連結截圖：" + "、".join(u for u, _ in shots))
        ok_urls = {u for u, _ in shots}
        for u, why in shot_fail:
            lines.append(f"📸 {why}（{u[:60]}）" if u in ok_urls else f"📸 截圖失敗：{why}（{u[:60]}）")
        for i, chunk in enumerate(daily.split_message("\n".join(lines))):
            if i == 0:
                files = [discord.File(io.BytesIO(png), filename=f"preview_{n}.jpg")
                         for n, (_, png) in enumerate(shots, 1)]
                await m.reply(chunk, files=files or None, mention_author=False, suppress_embeds=True)
                shots.clear()
            else:
                await m.channel.send(chunk, suppress_embeds=True)

    # ---------------------------------------------------------------- 總結預覽
    async def _recap(self, m: discord.Message, arg: str) -> None:
        tz = self.cfg.tz
        today = formatting.local_date(m.created_at, tz)
        target = None if arg == "全部" else (arg or today)
        msgs: list[discord.Message] = []
        async for h in m.channel.history(limit=200, before=m, oldest_first=False):
            if h.author.bot or h.content.strip().startswith("!"):
                continue
            if not h.content.strip() and not h.attachments:
                continue
            if target and formatting.local_date(h.created_at, tz) != target:
                continue
            msgs.append(h)
        msgs.reverse()
        if not msgs:
            await m.reply(f"找不到 {target or '最近'} 的內容 🤔", mention_author=False)
            return
        status = await m.reply(f"⏳ 準備總結 {len(msgs)} 則…", mention_author=False)
        items: list[daily.Item] = []
        for i, h in enumerate(msgs, 1):
            if h.id not in self.cache:
                await status.edit(content=f"⏳ 分析中 {i}/{len(msgs)}…")
            e = await self.enrich(h)
            items.append(daily.Item(e.time, e.kind, e.material() or "（空）", e.url))
        await status.edit(content=f"✍️ 撰寫總結中（{len(items)} 則材料）…")
        label = target or "最近 200 則"
        text = await self.llm.chat(daily.build_prompt(label), daily.build_material(items), max_tokens=2000)
        text = daily.link_citations(text, [it.url for it in items])
        out = f"📰 **每日總結預覽｜{label}**\n{text}"
        chunks = daily.split_message(out)
        await status.edit(content=chunks[0], suppress=True)
        for c in chunks[1:]:
            await m.channel.send(c, suppress_embeds=True)
