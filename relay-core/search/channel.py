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
from search.engine import WEEKDAYS, Candidate, Query, collect, score_text, search, select

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

    async def _send(self, m: discord.Message, text: str) -> None:
        chunks = daily.split_message(text)
        await m.reply(chunks[0], mention_author=False, suppress_embeds=True)
        for c in chunks[1:]:
            await m.channel.send(c, suppress_embeds=True)

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
                reply, parsed, n = await search(self.llm, q, source, now,
                                                thinking=getattr(self.cfg, "search_thinking", False))
            rng = f"{parsed.date_from or '最早'}～{parsed.date_to or '最新'}" if parsed.date_from or parsed.date_to else "全部時間"
            follow = f"｜追問改寫：{q}" if turns else ""
            footer = f"\n-# 搜尋：{where}｜範圍：{rng}｜搜尋詞：{'、'.join(parsed.terms[:8])}｜候選 {n} 則{follow}"
            await self._send(m, reply + footer)
            log.info("搜尋（%s）「%s」→ 候選 %d 則", where, q, n)
        except Exception as e:
            log.exception("搜尋失敗")
            await m.reply(f"⚠️ 搜尋失敗：{type(e).__name__}: {str(e)[:200]}", mention_author=False)
        finally:
            activity.finish(task)
