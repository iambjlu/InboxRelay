# relay-core/enrich/pipeline.py
"""正式流程的分析佇列（M4）：一則鏡像（可能含多則 Telegram 訊息）為一個工作。

1. 連結摘要、圖片摘要、檔案總結 → 合成一則回覆，貼在鏡像訊息下方（純文字訊息不回覆，避免洗版）。
2. 每則 Telegram 訊息抽關鍵字 → 編輯 #關鍵字庫 那筆紀錄補上 kw／gist，enriched 改 yes。
3. 分析結果餵給記憶（MemoryStore.observe）。
單線排隊：模型一次只做一件事。
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass

import discord

import activity

from config import Config
from dc_output import DiscordOut
from enrich.document import LEGACY_EXT, ext_of, is_supported, summarize_document
from enrich.image import MIME, summarize_image
from enrich.keywords import extract_keywords
from enrich.link import find_urls, summarize_link
from kwindex import KwIndex
from llm import LLM
from recap import daily

log = logging.getLogger("enrich")

MAX_FILE = 20 * 1024 * 1024


@dataclass
class EnrichJob:
    keys: list
    mirror_url: str


class Enricher:
    def __init__(self, cfg: Config, dc: DiscordOut, llm: LLM, index: KwIndex,
                 text_cache: dict, memory=None) -> None:
        self.cfg = cfg
        self.dc = dc
        self.llm = llm
        self.index = index
        self.text_cache = text_cache   # key → 完整原文（實時搬移時才有；匯入的用 excerpt）
        self.memory = memory
        self.queue: "asyncio.Queue[EnrichJob]" = asyncio.Queue()
        self._recent: deque[str] = deque(maxlen=3)
        self._queued: set[str] = set()

    def submit(self, keys: list, mirror_url: str) -> None:
        if mirror_url in self._queued:
            return
        self._queued.add(mirror_url)
        self.queue.put_nowait(EnrichJob(list(keys), mirror_url))

    @property
    def backlog(self) -> int:
        return self.queue.qsize()

    async def run(self) -> None:
        task, done = None, 0
        while True:
            if self.queue.empty() and task is not None:
                activity.finish(task, f"{done:,} 則")
                task, done = None, 0
            job = await self.queue.get()
            self._queued.discard(job.mirror_url)
            try:
                await activity.gate()                      # 手動暫停時在這裡等
                if task is None:
                    task = activity.start("AI 分析（摘要＋關鍵字）")
                e = self.index.get(job.keys[0]) if job.keys else None
                task.set(done=done, total=done + 1 + self.queue.qsize(),
                         detail=(e.rec.date if e else ""))
                activity.bind(task)
                await self.process(job)
                done += 1
                task.set(done=done)
            except Exception:
                log.exception("分析失敗：%s", job.mirror_url)
            finally:
                self.queue.task_done()

    def _text(self, key) -> str:
        e = self.index.get(key)
        return self.text_cache.get(key) or (e.rec.excerpt if e and e.rec.excerpt else "")

    async def process(self, job: EnrichJob) -> None:
        cfg = self.cfg
        keys = [k for k in job.keys if k in self.index]
        if not keys:
            return
        mirror = await self.dc.fetch_by_url(job.mirror_url)
        if mirror is None:
            log.info("鏡像已不存在，略過分析：%s", job.mirror_url)
            return
        texts = {k: self._text(k) for k in keys}
        parts: list[tuple[str, str]] = []
        # 細部進度：連結摘要＋附件＋截圖＋每則關鍵字，各算一步
        n_urls = len({u for t in texts.values() for u in find_urls(t)}) if cfg.link_summary else 0
        n_atts = sum(1 for a in mirror.attachments if a.size <= MAX_FILE)
        n_shot = 1 if (cfg.link_screenshot and cfg.ch_screenshot and find_urls(" ".join(texts.values()))) else 0
        activity.begin_item(min(n_urls, 3) + n_atts + n_shot + (len(keys) if cfg.keywords else 0))

        if cfg.link_summary:
            urls: list[str] = []
            for t in texts.values():
                for u in find_urls(t):
                    if u not in urls:
                        urls.append(u)
            for u in urls[:3]:
                parts.append((f"🔗 {u}", await summarize_link(self.llm, u)))
                activity.step()

        for a in mirror.attachments:
            name = a.filename
            if a.size > MAX_FILE:
                continue
            is_img = (a.content_type or "").startswith("image/") or ext_of(name) in MIME
            if is_img and cfg.image_summary:
                data = await a.read()
                caption = " ".join(texts.values())[:300]
                parts.append((f"🖼️ {name}", await summarize_image(self.llm, data, name, caption)))
            elif not is_img and cfg.document_summary and (is_supported(name) or ext_of(name) in LEGACY_EXT):
                data = await a.read()
                parts.append((f"📄 {name}", await summarize_document(self.llm, name, data)))
            data = b""
            activity.step()

        shot_urls: list[str] = []
        if cfg.link_screenshot and cfg.ch_screenshot:
            from enrich.screenshot import post_screenshots
            link_list = find_urls(" ".join(texts.values()))[:2]
            first = self.index.get(keys[0])
            shot_urls = await post_screenshots(self.dc, cfg.ch_screenshot, link_list,
                                               first.rec.date if first else "", job.mirror_url)
            activity.step()

        reply_url = None
        if parts:
            lines: list[str] = []
            for title, s in parts:
                lines.append(f"**{title}**")
                lines += [f"> {x}" for x in s.split("\n") if x.strip()]
            if shot_urls:
                lines.append("📸 截圖：" + " ".join(f"[↗{i}](<{u}>)" for i, u in enumerate(shot_urls, 1)))
            chunks = daily.split_message("\n".join(lines))
            reply = await mirror.reply(chunks[0], mention_author=False, suppress_embeds=True)
            for c in chunks[1:]:
                await mirror.channel.send(c, suppress_embeds=True)
            reply_url = reply.jump_url
        summaries = "；".join(f"{t}：{s}" for t, s in parts)

        for k in keys:
            kw, gist = [], ""
            material = "；".join(x for x in (texts[k], summaries) if x)
            if cfg.keywords and material.strip():
                kw, gist = await extract_keywords(self.llm, material, list(self._recent))
                activity.step()
                if self.memory and self.memory.observe(material, kw, gist):
                    asyncio.create_task(self.memory.upgrade(f"自動：累積 {len(self.memory.observations)} 則新分析"))
            if texts[k].strip():
                self._recent.append(texts[k][:200])
            await self.index.update(k, kw="、".join(kw) or None, gist=gist or None,
                                    enriched=True, reply=reply_url, shot=" ".join(shot_urls) or None)
        log.info("已分析 %s（%d 則，回覆 %s，佇列剩 %d）", job.mirror_url.rsplit("/", 1)[-1],
                 len(keys), "有" if reply_url else "無", self.queue.qsize())
