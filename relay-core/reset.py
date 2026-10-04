# relay-core/reset.py
"""清除測試資料，準備重新匯入（./run.sh reset）。

會刪掉：
- #投遞箱鏡像 的所有訊息（bot 的鏡像／AI 回覆，以及你在這裡發、已反向同步到 Telegram 的訊息；
  重新匯入時它們會從 Telegram 匯出檔回來，不刪會重複）
- bot 在 #關鍵字庫、#總結、連結預覽頻道（截圖與進度訊息）發的訊息
- #資料庫 的狀態紀錄：STATE、IMPORT、LIVE、RECAP_DIRTY、RECAP_NOTES
--memory：連 #記憶 一起清（包含你手動發的 #記憶）
--chats：連對話一起清：#搜尋、搜尋測試、AI 測試、通知與互動 頻道裡的所有訊息（你問的和 bot 回的）
--prompts：連 #提示詞 一起清（下次啟動會重新發一份預設提示詞）
--all：全部清空＝以上全部＋這些頻道裡任何人發的任何訊息（#投遞箱鏡像、#關鍵字庫、#資料庫、#總結、
       通知與互動、連結預覽、#搜尋、搜尋測試、AI 測試）
Telegram 那邊完全不動。
刪別人（你）的訊息需要 bot 有「管理訊息」權限；沒有的話會跳過並提示。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

import discord

log = logging.getLogger("reset")

STATE_TAGS = ("STATE", "IMPORT", "LIVE", "RECAP_DIRTY", "RECAP_NOTES")


SKIPPED = {"n": 0}     # 沒權限刪掉的（別人發的）訊息數


async def purge_bot_messages(ch: discord.TextChannel, bot_id: int | None, check=None) -> int:
    """兩週內的先試批次刪除（快，需要「管理訊息」權限）；沒權限或更舊的逐則刪。
    bot_id=None → 不分作者全部刪；刪不到別人的訊息就跳過並計數。"""
    def mine(m: discord.Message) -> bool:
        return (bot_id is None or m.author.id == bot_id) and (check is None or check(m))

    total = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=13, hours=12)
    try:
        deleted = await ch.purge(limit=None, check=mine, after=cutoff, bulk=True, oldest_first=False)
        total += len(deleted)
    except discord.Forbidden:
        cutoff = None                      # 沒權限批次刪 → 下面全部逐則刪
    async for m in ch.history(limit=None, before=cutoff):
        if mine(m):
            try:
                await m.delete()
                total += 1
            except discord.NotFound:
                pass
            except discord.Forbidden:
                SKIPPED["n"] += 1
    return total


async def run_reset(cfg, dc, yes: bool, memory: bool, chats: bool = False,
                    prompts: bool = False, wipe_all: bool = False) -> int:
    if wipe_all:
        memory = chats = prompts = True
    # (名稱, 頻道, 是否不分作者全刪)
    targets = [("#投遞箱鏡像", cfg.ch_inbox_mirror, True), ("#關鍵字庫", cfg.ch_keywords, False),
               ("#總結", cfg.ch_recap, False)]
    if wipe_all:
        targets = [(n, c, True) for n, c, _ in targets]
    if cfg.ch_screenshot:
        targets.append(("連結預覽與進度", cfg.ch_screenshot, wipe_all))
    if getattr(cfg, "ch_extra", None):
        targets.append(("#補充資料", cfg.ch_extra, wipe_all))
    if chats:
        for name, cid in (("#搜尋", cfg.ch_search), ("搜尋測試", cfg.ch_search_test),
                          ("AI 測試", cfg.ch_ai_test), ("通知與互動", cfg.ch_notify),
                          ("AI 對話", getattr(cfg, "ch_chat", None))):
            if cid and cid not in {t[1] for t in targets}:
                targets.append((name, cid, True))
    if wipe_all and getattr(cfg, "ch_chat_prompt", None):
        targets.append(("AI 對話提示詞與記憶", cfg.ch_chat_prompt, True))
    print("\n⚠️  即將刪除：")
    for name, _, everyone in targets:
        print(f"   • {name}" + ("（所有訊息）" if everyone else "（bot 發的訊息）"))
    if wipe_all:
        print("   • #資料庫（所有訊息：狀態紀錄、#記憶、#提示詞…）")
    else:
        print("   • #資料庫 的狀態紀錄（" + "、".join(STATE_TAGS) + "）" + ("＋所有 #記憶" if memory else "")
              + ("＋#提示詞" if prompts else ""))
    keep = ([] if prompts else ["#提示詞"]) + ([] if memory else ["#記憶"]) + ([] if chats else ["#搜尋與通知等對話"])
    print("   保留：" + ("、".join(keep) + "；" if keep else "") + "Telegram 不受影響。\n")
    if not yes:
        if not sys.stdin.isatty():
            print("需要確認：請加上 --yes")
            return 2
        ans = input("確定要刪除嗎？請輸入 RESET：").strip()
        if ans != "RESET":
            print("已取消")
            return 1
    bot_id = dc.client.user.id
    for name, cid, everyone in targets:
        before = SKIPPED["n"]
        n = await purge_bot_messages(await dc.channel(cid), None if everyone else bot_id)
        skipped = SKIPPED["n"] - before
        print(f"   🗑  {name}：刪除 {n} 則" + (f"；{skipped} 則你發的沒權限刪（bot 需要「管理訊息」），請手動刪" if skipped else ""))

    def is_state(m: discord.Message) -> bool:
        head = m.content.split("\n", 1)[0].strip()
        if wipe_all:
            return True
        if prompts and head.startswith("#提示詞"):
            return True
        if m.author.id == bot_id:
            return head in STATE_TAGS or (memory and head.startswith("#記憶"))
        return memory and head.startswith("#記憶")      # 你手動發的 #記憶
    before = SKIPPED["n"]
    n = await purge_bot_messages(await dc.channel(cfg.ch_db), None, is_state)
    if SKIPPED["n"] > before:
        print(f"   ⚠️  #資料庫：{SKIPPED['n'] - before} 則你發的訊息沒權限刪（bot 需要「管理訊息」），請手動刪")
    print(f"   🗑  #資料庫：刪除 {n} 則")
    print("\n✅ 清除完成。可以開始匯入了。")
    return 0
