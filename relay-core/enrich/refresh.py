# relay-core/enrich/refresh.py
"""一次性補做：舊的 AI 摘要回覆裡「讀不到的連結」重新摘要，直接編輯原本那則回覆。

- 網頁讀不到正文（「無法讀取」「僅根據網頁標題與描述」）→ 用網頁標籤＋Discord 網頁預覽＋截圖重寫。
  截圖優先用當初貼在連結預覽頻道的那張；沒有就現拍一張（不另外貼出來）。
- 連到圖片的網址（以前只寫「檔案下載連結｜image/…」）→ 改成圖片摘要，並交給本機文字辨識。
- 只處理摘要回覆的第一則訊息；新內容塞不下 2,000 字時從新摘要的尾巴刪行。
- 做完寫旗標 data_dir/link-refresh-v1，之後不再做；中途關掉 App 下次會從頭再掃（已修好的會跳過）。
"""
from __future__ import annotations

import asyncio
import logging
import re

import activity
import lanes
from enrich.link import embed_preview, find_urls, probe_link, summarize_probed

log = logging.getLogger("enrich.refresh")

FLAG = "link-refresh-v1"
LIMIT = 2000
_TITLE = re.compile(r"^\*\*(?:🔗|🖼️) (\S+)\*\*$")


def needs_refresh(body: str) -> bool:
    return ("無法讀取" in body or "（僅根據網頁標題與描述）" in body
            or ("檔案下載連結" in body and "類型：image/" in body))


def parse_segments(content: str) -> list[dict]:
    """把回覆拆成段落：{"url": 網址或 None, "lines": [...]}；連結段＝標題行＋後面的「> 」行。"""
    segs: list[dict] = []
    for ln in content.split("\n"):
        m = _TITLE.match(ln)
        if m:
            segs.append({"url": m.group(1), "lines": [ln]})
        elif ln.startswith("> ") and segs and segs[-1]["url"]:
            segs[-1]["lines"].append(ln)
        else:
            segs.append({"url": None, "lines": [ln]})
    return segs


def join_fit(segs: list[dict], changed: list[dict]) -> str:
    """組回訊息；超過 2,000 字就從新摘要裡最長的那段尾巴刪行。"""
    def text() -> str:
        return "\n".join(ln for s in segs for ln in s["lines"])
    out = text()
    while len(out) > LIMIT:
        big = max(changed, key=lambda s: len(s["lines"]), default=None)
        if big is None or len(big["lines"]) <= 2:
            return out[: LIMIT - 1] + "…"
        big["lines"].pop()
        out = text()
    return out


async def _old_shots(dc, entries) -> dict[str, bytes]:
    """當初貼在連結預覽頻道的截圖：網址 → 圖。"""
    out: dict[str, bytes] = {}
    seen: set[str] = set()
    for e in entries:
        for link in (e.rec.shot or "").split():
            if link in seen:
                continue
            seen.add(link)
            try:
                m = await dc.fetch_by_url(link)
            except Exception:
                continue
            if m is None or not m.attachments:
                continue
            for u in find_urls(m.content.split("\n", 1)[0]):
                if u not in out:
                    try:
                        out[u] = await m.attachments[0].read()
                    except Exception:
                        pass
    return out


async def refresh_one(dc, llm, index, cfg, reply_url: str, entries) -> tuple[bool, list[str]]:
    """回傳 (有沒有編輯, 新發現的圖片網址)。"""
    msg = await dc.fetch_by_url(reply_url)
    if msg is None:
        return False, []
    segs = parse_segments(msg.content)
    bad = [s for s in segs if s["url"] and needs_refresh("\n".join(s["lines"][1:]))]
    if not bad:
        return False, []
    mirror = await dc.fetch_by_url(entries[0].rec.mirror)
    shots = await _old_shots(dc, entries)
    caption = " ".join(e.rec.excerpt or "" for e in entries)[:300]
    changed, img_urls = [], []
    for s in bad:
        url = s["url"]
        probed = await probe_link(url)
        shot = shots.get(url)
        if shot is None and probed[1] not in ("image", "file", "private") and cfg.link_screenshot:
            from enrich.screenshot import capture
            shot = await capture(url)
        r = await summarize_probed(llm, url, probed, embed_preview(mirror, url) if mirror else "", shot,
                                   caption, describe_image=cfg.image_summary)
        if r.failed:
            continue                              # 還是讀不到：維持原樣
        s["lines"] = [f"**{r.title}**"] + [f"> {x}" for x in r.summary.split("\n") if x.strip()]
        changed.append(s)
        if r.image is not None:
            img_urls.append(url)
    shots.clear()
    if not changed:
        return False, []
    content = join_fit(segs, changed)
    if content == msg.content:
        return False, img_urls
    await msg.edit(content=content)
    return True, img_urls


_task = None            # 正在跑的 refresh_links（按「跳過」時取消）


def request_skip() -> bool:
    """跳過積欠的補做連結摘要：取消正在跑的那一輪，並寫下完成旗標，以後不再補做。回傳是否有東西被取消。"""
    from paths import data_dir
    try:
        (data_dir() / FLAG).write_text("done\n")
    except OSError:
        pass
    if _task is not None and not _task.done():
        _task.cancel()
        return True
    return False


async def refresh_links(dc, llm, index, cfg, on_link_images=None) -> None:
    from paths import data_dir
    flag = data_dir() / FLAG
    if flag.exists() or not cfg.link_summary:
        return
    groups: dict[str, list] = {}
    for e in list(index.entries.values()):
        if e.rec.reply and e.rec.excerpt and "http" in e.rec.excerpt:
            groups.setdefault(e.rec.reply, []).append(e)
    if groups:
        global _task
        _task = asyncio.current_task()
        task = None
        fixed = 0
        try:
            await activity.gate()
            task = activity.start("補做連結摘要", total=len(groups))
            for reply_url, ents in groups.items():
                await activity.gate()
                task.set(detail=ents[0].rec.date)
                while True:
                    try:
                        edited, imgs = await refresh_one(dc, llm, index, cfg, reply_url, ents)
                        fixed += edited
                        for e in ents:
                            mine = [u for u in imgs if u in find_urls(e.rec.excerpt or "")]
                            if mine and not e.rec.img:
                                await index.update(e.rec.key, img=" ".join(mine))
                        if imgs and on_link_images is not None:
                            on_link_images(ents[0].rec.mirror)
                    except Exception as ex:
                        from llm import is_offline_error
                        if is_offline_error(ex):           # 外部 LLM 連不上 → 等下次開始時間再從這則接著做
                            lane = lanes.get("llm")
                            if lane is not None:
                                lane.go_offline(type(ex).__name__)
                            await activity.gate()
                            continue
                        log.exception("補做連結摘要失敗：%s", reply_url)
                    break
                task.advance()
        except asyncio.CancelledError:
            if not flag.exists():                  # 不是按「跳過」造成的取消（例如關閉核心）：照常往外丟
                raise
            log.info("補做連結摘要：已依要求跳過（更新 %d 則）", fixed)
            return
        finally:
            _task = None
            if task is not None:
                activity.finish(task, f"更新 {fixed} 則")
        log.info("補做連結摘要完成：更新 %d 則（檢查 %d 則）", fixed, len(groups))
    try:
        flag.write_text("done\n")
    except OSError:
        pass
