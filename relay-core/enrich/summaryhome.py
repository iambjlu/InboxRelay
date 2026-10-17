# relay-core/enrich/summaryhome.py
"""AI 摘要（圖片分析、網頁摘要）的家：貼在「連結預覽與進度」頻道，鏡像訊息最後只留一行小字箭頭 `-# [↗ 摘要](<連結>)`。

- 只有 bot 自己發的鏡像（可以編輯）、而且有設定預覽頻道才搬；使用者自己在 Discord 發的鏡像照舊把摘要回覆在訊息下面。
- rec.reply 存摘要第一則的連結（不管在哪個頻道），刪除同步／重新分析都靠它刪舊的。
- migrate_recent()：一次性，把最近 MOVE_DAYS 天還回覆在鏡像下面的摘要搬過去。
"""
from __future__ import annotations

import asyncio
import logging

import discord

import records
from recap import daily

log = logging.getLogger("summaryhome")

ARROW_PREFIX = "-# [↗ 摘要]("
HEAD = "🧾 摘要｜"
MOVE_DAYS = 7
FLAG = "summary-home-v1"


def put_arrow(content: str, url: str) -> str:
    """鏡像內容最後放一行摘要箭頭（先清掉舊的，重複執行結果一樣）。"""
    lines = [ln for ln in (content or "").split("\n") if not ln.startswith(ARROW_PREFIX)]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines + [f"{ARROW_PREFIX}<{url}>)"])


def can_move(dc, cfg, mirror) -> bool:
    if not getattr(cfg, "ch_screenshot", None) or mirror is None:
        return False
    me = dc.client.user
    return bool(me is not None and mirror.author.id == me.id and mirror.channel.id == cfg.ch_inbox_mirror)


def with_head(mirror_url: str, body: str) -> str:
    return f"{HEAD}[↗ 跳到鏡像](<{mirror_url}>)\n{body}"


async def post(dc, cfg, mirror, body: str) -> list:
    """把摘要發到預覽頻道（超過 2000 字分多則），鏡像加上箭頭。回傳發出的訊息（第一則在最前面）。"""
    ch = await dc.channel(cfg.ch_screenshot)
    msgs = []
    for c in daily.split_message(with_head(mirror.jump_url, body)):
        msgs.append(await ch.send(c, suppress_embeds=True))      # type: ignore[attr-defined]
    await set_arrow(mirror, msgs[0].jump_url)
    return msgs


async def set_arrow(mirror, url: str) -> bool:
    try:
        new = put_arrow(mirror.content, url)
        if new == (mirror.content or "") or len(new) > 2000:
            return False
        await mirror.edit(content=new)
        return True
    except discord.HTTPException:
        log.warning("鏡像加摘要箭頭失敗：%s", mirror.jump_url, exc_info=True)
        return False


async def migrate_recent(dc, cfg, index) -> int:
    """一次性：最近 MOVE_DAYS 天、回覆在鏡像下面的摘要搬到預覽頻道（舊的刪掉，紀錄的 reply 改指新的）。回傳搬了幾則。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from paths import data_dir
    flag = data_dir() / FLAG
    if flag.exists() or not cfg.ch_screenshot or index is None:
        return 0
    since = (datetime.now(ZoneInfo(cfg.tz)) - timedelta(days=MOVE_DAYS)).strftime("%Y-%m-%d")
    groups: dict[str, list] = {}
    for e in index.entries.values():
        if e.rec.reply and e.rec.date[:10] >= since and records.parse_message_url(e.rec.reply):
            ids = records.parse_message_url(e.rec.reply)
            if ids and ids[0] == cfg.ch_inbox_mirror:
                groups.setdefault(e.rec.reply, []).append(e)
    moved = 0
    for reply_url, ents in sorted(groups.items(), key=lambda kv: min(e.rec.date for e in kv[1])):
        try:
            if await _move_one(dc, cfg, index, reply_url, ents):
                moved += 1
        except Exception:
            log.exception("搬移摘要失敗：%s", reply_url)
            return moved                                         # 不寫旗標，下次啟動接著做
        await asyncio.sleep(1.0)
    flag.write_text("done")
    log.info("🧾 已把最近 %d 天的 %d 則 AI 摘要搬到連結預覽頻道", MOVE_DAYS, moved)
    return moved


async def _move_one(dc, cfg, index, reply_url: str, ents: list) -> bool:
    rm = await dc.fetch_by_url(reply_url)
    mirror = await dc.fetch_by_url(ents[0].rec.mirror)
    if rm is None or not can_move(dc, cfg, mirror):
        return False
    me = dc.client.user
    extra = []                                                   # 超過 2000 字時接在後面的幾則（沒有回覆對象、不是鏡像）
    async for nxt in rm.channel.history(limit=8, after=rm, oldest_first=True):
        if (nxt.author.id != me.id or nxt.reference is not None or nxt.attachments
                or index.by_mirror(nxt.jump_url) or formatting_header(nxt.content)):
            break
        extra.append(nxt)
    body = "\n".join([rm.content] + [m.content for m in extra])
    msgs = await post(dc, cfg, mirror, body)
    for e in ents:
        await index.update(e.rec.key, reply=msgs[0].jump_url)
    for old in [rm] + extra:
        try:
            await old.delete()
        except discord.NotFound:
            pass
    return True


def formatting_header(content: str) -> bool:
    import formatting
    return formatting.parse_header_date(content) is not None
