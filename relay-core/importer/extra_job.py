# relay-core/importer/extra_job.py
"""./run.sh import-extra：把補充資料（發票、JSONL…）發到 #補充資料 並寫進 #關鍵字庫。"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

import records
import state
from importer.extra import DayDoc, doc_key, post_chunks, scan
from kwindex import KwIndex

log = logging.getLogger("import-extra")
SEND_INTERVAL = 0.5      # 補充資料與 #關鍵字庫 交替發，每個頻道約每秒 1 則（Discord 上限約 5 則／5 秒）


def _p(s: str) -> None:
    print(s, flush=True)


def report(res, path: Path) -> None:
    by_kind: dict[str, list[DayDoc]] = {}
    for d in res.docs:
        by_kind.setdefault(d.kind, []).append(d)
    _p(f"📂 {path}")
    for kind, docs in by_kind.items():
        icon, name, _ = docs[0].meta()
        _p(f"   {icon} {name}：{res.used.get(kind, 0)} 個檔案 → {len(docs):,} 天（{docs[0].day} ~ {docs[-1].day}）")
    if not res.docs:
        _p("   （沒有找到可以匯入的資料）")
    if res.undated:
        _p(f"   ⚠️ {len(res.undated)} 個 JSONL 檔沒有可用的資料（每行要有 day 與 body）：")
        for x in res.undated[:10]:
            _p(f"      · {x}")
    if res.skipped:
        _p(f"   ⏭  {len(res.skipped)} 個檔案不認得格式，略過（例：{'、'.join(res.skipped[:5])}）")


async def run_extra(cfg, dc, path: str, date_from: str | None, date_to: str | None, dry_run: bool) -> int:
    p = Path(path).expanduser()
    if not p.exists():
        _p(f"❌ 找不到 {p}")
        return 2
    res = scan(p, date_from, date_to)
    report(res, p)
    if dry_run:
        if res.docs:
            ex = res.docs[0]
            _p("\n🔎 範例（第一天的貼文）：\n" + post_chunks(ex)[0][:800])
            _p(f"\n🔎 給 AI／總結／搜尋的內容：\n{ex.excerpt[:400]}")
        _p("\n（試跑：沒有發出任何訊息）")
        return 0
    if not res.docs:
        return 0
    if not cfg.ch_extra:
        _p("❌ 還沒設定補充資料頻道：在 .env（或 App 設定）填 DC_CH_EXTRA=頻道 ID")
        return 2
    problems = await dc.check_channels({"補充資料": cfg.ch_extra, "#關鍵字庫": cfg.ch_keywords, "#資料庫": cfg.ch_db})
    if problems:
        for x in problems:
            _p(f"❌ {x}")
        return 2

    bot_id = dc.client.user.id
    _p("🔎 讀取 #關鍵字庫（去重用，紀錄多時要 1～3 分鐘）…")
    index = KwIndex(await dc.channel(cfg.ch_keywords), bot_id)
    await index.load()
    dirty = state.DirtyDays(state.DbRecord(await dc.channel(cfg.ch_db), bot_id, "RECAP_DIRTY"))
    await dirty.load()

    new = changed = same = 0
    touched: set[str] = set()
    started = time.monotonic()
    for i, doc in enumerate(res.docs, 1):
        key = doc_key(doc.kind, doc.day, 0)
        old = index.get(key)
        fp = doc.fingerprint
        if old is not None and old.rec.fid == fp:
            same += 1
            continue
        if old is not None:                                 # 內容變了 → 刪掉舊的貼文與紀錄
            for u in [old.rec.mirror, old.rec.reply] + (old.rec.shot or "").split():
                if u:
                    await dc.delete_by_url(u)
            await index.delete(key)
            changed += 1
        else:
            new += 1
        msgs = []
        for chunk in post_chunks(doc):
            msgs.append(await dc.send(cfg.ch_extra, chunk))
            await asyncio.sleep(SEND_INTERVAL)
        _, _, hhmm = doc.meta()
        rec = records.KwRecord(
            tg_chat=key[0], tg_msg=key[1], origin="extra", date=f"{doc.day} {hhmm}", type=doc.kind,
            mirror=msgs[0].jump_url, shot=" ".join(m.jump_url for m in msgs[1:]) or None, fid=fp,
            excerpt=doc.excerpt)
        kmsg = await dc.send(cfg.ch_keywords, records.format_kw(rec))
        index.add(rec, kmsg)
        touched.add(doc.day)
        await asyncio.sleep(SEND_INTERVAL)
        if i % 10 == 0 or i == len(res.docs):
            el = time.monotonic() - started
            eta = el / i * (len(res.docs) - i)
            sys.stdout.write(f"\r⏳ {i:,}/{len(res.docs):,}（{100 * i / len(res.docs):.1f}%）{doc.day}"
                             f"｜剩約 {int(eta // 60)} 分   ")
            sys.stdout.flush()
    _p("")
    if touched:
        await dirty.mark_many(sorted(touched))
        _p("   核心（App）若開著，5 分鐘內會讀到這些新紀錄，排進夜間 AI 工作。")
    _p(f"✅ 完成：新增 {new:,} 天、更新 {changed:,} 天、沒變略過 {same:,} 天")
    if touched:
        _p(f"   {len(touched):,} 天已標記「需要重新總結」；AI 分析與日／月／年總結會在下一次每日 AI 時段（{cfg.recap_time}）處理，"
           "想馬上做就按 App 的「立即總結」。")
    return 0
