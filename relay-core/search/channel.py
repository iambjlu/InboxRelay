# relay-core/search/channel.py
"""搜尋頻道：直接用自然語言問（例如「9/30 吃了什麼」），不用斜線指令。

追問：對 bot 的回答按「回覆」。只沿回覆鏈往回看最多 3 輪（每段截短），
頻道裡累積再多搜尋紀錄也不會讓模型看的資料變多。Qwen 先判斷追問是
「一般知識」（直接回答並標明）還是「再搜資料」（把問題補完整後重新搜尋）。

- 正式 #搜尋（SEARCH_LIVE=true）：搜 #關鍵字庫 與 #總結。
- 搜尋測試頻道：搜「AI 測試頻道」裡丟過的東西、AI 分析回覆、!總結 的每日總結預覽。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import formatting
import activity
import prompts
from recap import daily
from collections import OrderedDict
from dataclasses import dataclass, field

from search.engine import (MAX_CANDIDATES, WEEKDAYS, Candidate, Query, answer_page, collect, page, score_text,
                           search_ranked, select)

MAX_TURNS = 3          # 追問最多往回看幾輪（一問一答算一輪）
TURN_CHARS = 600       # 每段對話截短到幾字


def clean_bot_text(text: str) -> str:
    """去掉小字註腳與連結網址，只留給人看的內容。"""
    lines = [ln for ln in text.split("\n") if not ln.startswith("-# ")]
    t = "\n".join(lines)
    t = re.sub(r"\[(↗[^\]]*)\]\(<[^>]+>\)", r"\1", t)
    return t.strip()


def parse_followup(out: str, fallback: str) -> tuple[str, str]:
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            mode = "general" if str(d.get("mode", "")).startswith("general") else "search"
            return mode, str(d.get("query") or fallback).strip()
        except json.JSONDecodeError:
            pass
    return "search", fallback

log = logging.getLogger("search")

MORE_PREFIX = "inboxrelay:search_more:"
MORE_KEEP = 40          # 記憶體裡最多留幾次搜尋的「還沒看的候選」（只留最近的）
_MORE_HANDLER = [None]  # SearchChannel.on_more（按鈕是持久的，處理函式由頻道物件註冊）


class MoreButton(discord.ui.DynamicItem[discord.ui.Button], template=MORE_PREFIX + r"(?P<token>[0-9a-f]{8})"):
    """「顯示更多」按鈕：custom_id 帶一個短代碼，對應記憶體裡這次搜尋還沒看的候選。App 重開後按鈕還在，但資料沒了會提示重新問。"""

    def __init__(self, token: str, label: str = "顯示更多") -> None:
        super().__init__(discord.ui.Button(label=label, emoji="➕", style=discord.ButtonStyle.secondary,
                                           custom_id=MORE_PREFIX + token))
        self.token = token

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match):
        return cls(match["token"])

    async def callback(self, interaction: discord.Interaction) -> None:
        h = _MORE_HANDLER[0]
        if h is None:
            await interaction.response.send_message("還沒準備好，請稍後再按", ephemeral=True)
            return
        await h(interaction, self.token)


@dataclass
class MoreState:
    question: str
    query: Query
    rest: list = field(default_factory=list)      # 還沒看的候選（已依關聯度排序）
    offset: int = 0                               # 已經看了幾筆（＝下一批的名次起點）
    total: int = 0
    where: str = ""
    thinking: bool = False
    busy: bool = False


def more_view(token: str, remaining: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(MoreButton(token, f"顯示更多（還有 {remaining:,} 則，每次 {MAX_CANDIDATES} 則）"))
    return v


class SearchChannel:
    def __init__(self, cfg, dc, llm, index, recap_channel, live_ids: set[int],
                 test_ids: set[int], aitest=None) -> None:
        self.cfg = cfg
        self.dc = dc
        self.llm = llm
        self.index = index
        self.rc = recap_channel     # recap.runner.RecapChannel（可為 None）
        self.live_ids = live_ids
        self.test_ids = test_ids
        self.aitest = aitest        # aitest.AiTestChannel（取分析快取）
        dc.add_message_handler(self.on_message)
        self._more: "OrderedDict[str, MoreState]" = OrderedDict()
        _MORE_HANDLER[0] = self.on_more
        try:
            dc.client.add_dynamic_items(MoreButton)         # 「顯示更多」按鈕（持久；App 重開後仍能收到點擊）
        except Exception:
            log.warning("註冊「顯示更多」按鈕失敗", exc_info=True)

    # ------------------------------------------------------------ 資料來源
    def _recaps(self) -> dict[str, tuple[str, str]]:
        out = {}
        if self.rc is None:
            return out
        for (kind, key), parts in self.rc.posts.items():
            if kind == "DAILY" and parts and self.rc.has(kind, key):      # 跳過佔位
                out[key] = (self.rc.body(kind, key), parts[min(parts)].jump_url)
        return out

    async def _live_source(self, q: Query) -> list[Candidate]:
        return collect(list(self.index.entries.values()), self._recaps(), q)

    async def _test_source(self, q: Query) -> list[Candidate]:
        """讀 AI 測試頻道最近 1000 則：使用者丟的內容＋AI 分析回覆＋每日總結預覽。"""
        if not self.cfg.ch_ai_test:
            return []
        ch = await self.dc.channel(self.cfg.ch_ai_test)
        tz = self.cfg.tz
        cache = self.aitest.cache if self.aitest else {}
        cands: list[Candidate] = []
        async for m in ch.history(limit=1000):
            when = formatting.local_dt(m.created_at, tz).strftime("%Y-%m-%d %H:%M")
            body = m.content.strip()
            if m.author.bot:
                if body.startswith("📰"):
                    kind, text, ex = "daily", body, "每日總結預覽"
                elif body.startswith("🤖"):
                    kind, text, ex = "analysis", body, body.split("\n", 2)[1][:40] if "\n" in body else body[:40]
                else:
                    continue
            else:
                if body.startswith("!") or (not body and not m.attachments):
                    continue
                names = "、".join(a.filename for a in m.attachments)
                e = cache.get(m.id)
                extra = []
                if names:
                    extra.append(f"附件：{names}")
                if e:
                    extra += [f"{t}：{s}" for t, s in e.parts]
                    if e.gist:
                        extra.append(f"重點：{e.gist}")
                    if e.kw:
                        extra.append(f"關鍵字：{'、'.join(e.kw)}")
                text = "｜".join([body] + extra if body else extra) or "（無文字）"
                kind = "image" if any((a.content_type or "").startswith("image/") for a in m.attachments) \
                    else ("file" if m.attachments else "text")
                ex = (body[:20] + ("…" if len(body) > 20 else "")) if body else (e.gist if e and e.gist else names)
            cands.append(Candidate(when, kind, text[:3000], ex, m.jump_url, score_text(text, q.terms)))
        return select(cands, q)

    async def _send(self, m: discord.Message, text: str, view: discord.ui.View | None = None) -> discord.Message:
        """回覆並依長度拆成多則；view（按鈕）附在最後一則。回傳最後一則。"""
        chunks = daily.split_message(text)
        last = None
        for i, c in enumerate(chunks):
            v = view if i == len(chunks) - 1 else None
            kw = {"view": v} if v is not None else {}
            if i == 0:
                last = await m.reply(c, mention_author=False, suppress_embeds=True, **kw)
            else:
                last = await m.channel.send(c, suppress_embeds=True, **kw)
        return last

    def _remember_more(self, st: MoreState) -> str:
        import secrets
        token = secrets.token_hex(4)
        self._more[token] = st
        while len(self._more) > MORE_KEEP:
            self._more.popitem(last=False)
        return token

    async def on_more(self, interaction: discord.Interaction, token: str) -> None:
        """按了「顯示更多」：把下一批（關聯度排名接下來的 MAX_CANDIDATES 則）交給模型，結果貼成新的一則回覆。"""
        st = self._more.get(token)
        if st is None:
            await interaction.response.send_message("這次搜尋的資料已經不在了（App 重開過或太久以前），請再問一次", ephemeral=True)
            return
        if st.busy:
            await interaction.response.send_message("正在整理上一批，請稍等", ephemeral=True)
            return
        st.busy = True
        await interaction.response.defer()
        task = activity.start(f"搜尋（更多）：{st.question[:20]}", background=False)
        try:
            batch = page(st.rest, 0)
            lo, hi = st.offset + 1, st.offset + len(batch)
            reply = await answer_page(self.llm, st.question, batch, st.thinking)
            st.rest = st.rest[len(batch):]
            st.offset = hi
            footer = f"\n-# 搜尋：{st.where}｜關聯度第 {lo:,}～{hi:,} 名（共命中 {st.total:,} 則）｜搜尋詞：{'、'.join(st.query.terms[:8])}"
            view = None
            if st.rest:
                view = more_view(token, len(st.rest))
            else:
                footer += "｜已經是最後一批"
            msg = interaction.message
            try:
                await msg.edit(view=None)                     # 這顆按鈕用掉了（下一批的按鈕在新的回覆上）
            except discord.HTTPException:
                pass
            await self._send(msg, reply + footer, view)
            if not st.rest:
                self._more.pop(token, None)
            log.info("搜尋「%s」顯示更多：第 %d～%d 名", st.question[:30], lo, hi)
        except Exception as e:
            log.exception("顯示更多失敗")
            await interaction.followup.send(f"⚠️ 失敗：{type(e).__name__}: {str(e)[:150]}", ephemeral=True)
        finally:
            st.busy = False
            activity.finish(task)

    # ------------------------------------------------------------ 處理
    async def on_message(self, m: discord.Message) -> None:
        cid = m.channel.id
        if (cid not in self.live_ids and cid not in self.test_ids) or m.author.bot:
            return
        q = m.content.strip()
        if not q or q.startswith("!"):
            return
        asyncio.create_task(self._answer(m, q, test=cid in self.test_ids))

    async def _conversation(self, m: discord.Message) -> list[tuple[str, str]]:
        """沿回覆鏈往回收集對話（舊→新），最多 MAX_TURNS 輪；不是回覆 bot 就回傳空。"""
        turns: list[tuple[str, str]] = []
        cur = m
        me = self.dc.client.user
        for _ in range(MAX_TURNS * 2):
            ref = cur.reference
            if ref is None or ref.message_id is None:
                break
            try:
                prev = ref.resolved if isinstance(ref.resolved, discord.Message) \
                    else await cur.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                break
            who = "助手" if (me and prev.author.id == me.id) else "使用者"
            if cur is m and who != "助手":
                return []           # 回覆的不是 bot → 當成新搜尋
            text = clean_bot_text(prev.content) if who == "助手" else prev.content.strip()
            turns.append((who, text[:TURN_CHARS]))
            cur = prev
        turns.reverse()
        return turns

    async def _answer(self, m: discord.Message, q: str, test: bool) -> None:
        task = activity.start(f"搜尋：{q[:20]}", background=False)
        try:
            async with m.channel.typing():
                now = datetime.now(ZoneInfo(self.cfg.tz))
                where = "AI 測試頻道" if test else "投遞箱"
                turns = await self._conversation(m)
                if turns:
                    convo = "\n".join(f"{w}：{t}" for w, t in turns) + f"\n使用者（追問）：{q}"
                    out = await self.llm.chat(
                        prompts.render("SEARCH_FOLLOWUP", today=now.strftime("%Y-%m-%d"),
                                       weekday="星期" + WEEKDAYS[now.weekday()]), convo, max_tokens=300)
                    mode, query = parse_followup(out, q)
                    if mode == "general":
                        ans = await self.llm.chat(prompts.get("GENERAL_ANSWER"), convo, max_tokens=900)
                        footer = f"\n-# 💡 一般知識回答（不是從你的資料找的）｜追問第 {len(turns) // 2 + 1} 輪"
                        await self._send(m, ans + footer)
                        log.info("追問（一般知識）「%s」", q)
                        return
                    q = query      # 補完整後重新搜尋
                source = self._test_source if test else self._live_source
                thinking = getattr(self.cfg, "search_thinking", False)
                res = await search_ranked(self.llm, q, source, now, thinking=thinking)
            parsed, n = res.query, res.total
            rng = f"{parsed.date_from or '最早'}～{parsed.date_to or '最新'}" if parsed.date_from or parsed.date_to else "全部時間"
            follow = f"｜追問改寫：{q}" if turns else ""
            shown = f"，先看關聯度最高的 {res.shown} 則" if res.rest else ""
            footer = f"\n-# 搜尋：{where}｜範圍：{rng}｜搜尋詞：{'、'.join(parsed.terms[:8])}｜命中 {n} 則{shown}{follow}"
            view = None
            if res.rest:
                token = self._remember_more(MoreState(q, parsed, res.rest, res.shown, n, where, thinking))
                view = more_view(token, len(res.rest))
            await self._send(m, res.reply + footer, view)
            log.info("搜尋（%s）「%s」→ 命中 %d 則（先看 %d 則）", where, q, n, res.shown)
        except Exception as e:
            log.exception("搜尋失敗")
            await m.reply(f"⚠️ 搜尋失敗：{type(e).__name__}: {str(e)[:200]}", mention_author=False)
        finally:
            activity.finish(task)
