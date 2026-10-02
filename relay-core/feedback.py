# relay-core/feedback.py
"""通知頻道互動：用一般的話告訴 bot 哪裡要改。

例：「9/30 的每日總結沒有出現滿漢大餐，要補上」
  → 找出日期 → 把要求記到 #資料庫 RECAP_NOTES（之後重新總結也會保留）→ 改寫那天的總結並回覆連結。
例：「以後總結都要把吃的東西列出來」→ 寫進記憶，之後所有分析與總結都會參考。
只接受伺服器擁有者與 MEMORY_EDITORS 的留言。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import activity
import prompts
from search.engine import WEEKDAYS

log = logging.getLogger("feedback")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")


def parse_route(out: str) -> dict:
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                return d
        except json.JSONDecodeError:
            pass
    return {"action": "other", "reply": "我沒看懂 😅 可以說說是哪一天的總結、哪裡要改嗎？"}


class FeedbackChannel:
    def __init__(self, cfg, dc, llm, recapper, memory, editors: set[int], channel_id: int) -> None:
        self.cfg = cfg
        self.llm = llm
        self.recapper = recapper
        self.memory = memory
        self.editors = editors
        self.channel_id = channel_id
        dc.add_message_handler(self.on_message)

    async def on_message(self, m: discord.Message) -> None:
        if m.channel.id != self.channel_id or m.author.bot or m.author.id not in self.editors:
            return
        if not m.content.strip():
            return
        asyncio.create_task(self._handle(m))

    async def _handle(self, m: discord.Message) -> None:
        task = activity.start(f"通知頻道：{m.content.strip()[:20]}", background=False)
        try:
            async with m.channel.typing():
                await self._route(m)
        except Exception as e:
            log.exception("通知頻道處理失敗")
            await m.reply(f"⚠️ 處理失敗：{type(e).__name__}: {str(e)[:200]}", mention_author=False)
        finally:
            activity.finish(task)

    async def _route(self, m: discord.Message) -> None:
        now = datetime.now(ZoneInfo(self.cfg.tz))
        out = await self.llm.chat(
            prompts.render("FEEDBACK_ROUTE", today=now.strftime("%Y-%m-%d"), weekday="星期" + WEEKDAYS[now.weekday()]),
            m.content.strip(), max_tokens=400)
        d = parse_route(out)
        action = str(d.get("action", "other"))
        instruction = str(d.get("instruction") or m.content.strip())
        note = str(d.get("memory_note") or "").strip()
        r = self.recapper
        replies: list[str] = []

        if action == "fix_daily" and r is not None:
            day = str(d.get("date") or "")
            if not _DAY.match(day):
                replies.append("我抓不到是哪一天 🤔 可以寫清楚日期嗎？例如「9/30 的總結…」")
            elif not r._entries(day):
                replies.append(f"{day} 在 #關鍵字庫 沒有任何訊息紀錄，沒辦法總結那天。")
            else:
                await r.add_note(day, instruction)
                async with r._lock:
                    await r.summarize_day(day)
                    month_done = r.rc.has("MONTHLY", day[:7])
                    if month_done:
                        await r.summarize_month(day[:7])
                first = r.rc.first("DAILY", day)
                link = f"[↗ 看新版]({first.jump_url})" if first else ""
                replies.append(f"✅ 已依你的要求改寫 **{day}** 每日總結 {link}\n-# 要求：{instruction}"
                               + ("\n-# 該月月總結也一併更新了" if month_done else ""))
        elif action == "fix_monthly" and r is not None:
            month = str(d.get("month") or "")
            if not _MONTH.match(month) or not r.rc.has("MONTHLY", month):
                replies.append("找不到那個月的月總結 🤔（月總結要等整個月的每日總結都完成才會產生）")
            else:
                await r.add_note(month, instruction)
                async with r._lock:
                    await r.summarize_month(month)
                first = r.rc.first("MONTHLY", month)
                replies.append(f"✅ 已改寫 **{month}** 月總結 [↗ 看新版]({first.jump_url})" if first else "✅ 已改寫")
        elif action not in ("fix_daily", "fix_monthly", "memory"):
            replies.append(str(d.get("reply") or "我可以幫你修改每日／月總結，或記住你的用語與偏好。"))

        if note and self.memory is not None:
            self.memory.add_correction(note)
            replies.append(f"🧠 已記下：{note}（記憶升級後會在這裡通知）")
            asyncio.create_task(self.memory.upgrade("通知頻道回饋"))
        elif action == "memory":
            replies.append("我沒抓到要記住的內容 🤔")

        await m.reply("\n".join(replies), mention_author=False, suppress_embeds=True)
