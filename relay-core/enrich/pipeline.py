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
import lanes
from ocr import match_attachments, useful

from config import Config
from dc_output import DiscordOut
from enrich.document import LEGACY_EXT, ext_of, is_supported, summarize_document
from enrich.image import MIME, is_converted_copy, summarize_image
from enrich.keywords import extract_keywords
from enrich.link import embed_preview, find_urls, probe_link, summarize_probed
from kwindex import KwIndex
from llm import LLM
from recap import daily

log = logging.getLogger("enrich")

MAX_FILE = 20 * 1024 * 1024


def ocr_caption(ocr_txt: str) -> str:
    """給圖片分析的「圖中文字」說明。新的 OCR 在聊天截圖會帶〔左〕〔右〕（該行文字在畫面左／右側），舊的沒有。"""
    if "〔左〕" in ocr_txt or "〔右〕" in ocr_txt:
        return ("圖中文字（本機辨識；行首的〔左〕〔右〕是該行在畫面左側／右側，聊天截圖的右側是使用者本人、左側是對方，"
                "沒有標記的行位置不明；僅供參考，仍以圖片為準）：" + ocr_txt[:1200])
    return "圖中文字（本機辨識，只有字、沒有左右位置，不能用來判斷是誰說的）：" + ocr_txt[:800]


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
        self._front: deque[EnrichJob] = deque()   # 外部 LLM 連不上而退回來的工作：恢復後最先做
        self._posted: list = []                  # 這則工作已經發出去的訊息（連不上時收回，避免重做時重複）
        self._holding = False          # 手上有一則正在做（或在等夜間時段）
        self.on_link_images = None     # callable(mirror_url)：有連到圖片的網址時通知文字辨識
        self.ocr_worker = None         # OcrWorker：分析圖片前先等這則的圖中文字辨識完（本機 ANE 車道在跑時），當分析的參考
        self.shot_worker = None        # ShotWorker：連結截圖車道（有自己的排程；分析時車道開著就先截圖當輔助）

    def submit(self, keys: list, mirror_url: str) -> None:
        if mirror_url in self._queued:
            return
        self._queued.add(mirror_url)
        self.queue.put_nowait(EnrichJob(list(keys), mirror_url))
        if self.shot_worker is not None:
            self.shot_worker.submit(mirror_url)

    @property
    def backlog(self) -> int:
        return self.queue.qsize() + len(self._front) + (1 if self._holding else 0)

    @property
    def drained(self) -> bool:
        return self.queue.empty() and not self._front and not self._holding

    async def run(self) -> None:
        task, done = None, 0
        while True:
            if self.queue.empty() and not self._front and task is not None:
                activity.finish(task, f"{done:,} 則")
                task, done = None, 0
            from_queue = not self._front
            job = await self.queue.get() if from_queue else self._front.popleft()
            self._holding = True
            self._queued.discard(job.mirror_url)
            try:
                await activity.gate()                      # 手動暫停、不在時段、或外部 LLM 離線時在這裡等
                lane = lanes.get("llm")
                if lane is not None:
                    await lane.gate()
                if task is None:
                    task = activity.start("AI 分析（摘要＋關鍵字）")
                e = self.index.get(job.keys[0]) if job.keys else None
                task.set(done=done, total=done + 1 + self.queue.qsize() + len(self._front),
                         detail=(e.rec.date if e else ""))
                activity.bind(task)
                self._posted = []
                await self.process(job)
                done += 1
                task.set(done=done)
            except Exception as ex:
                from llm import is_offline_error
                if is_offline_error(ex):
                    await self._undo_posted()
                    self._front.appendleft(job)            # 留著：恢復連線後從這則接著做
                    self._queued.add(job.mirror_url)
                    lane = lanes.get("llm")
                    if lane is not None:
                        lane.go_offline(type(ex).__name__)
                else:
                    log.exception("分析失敗：%s", job.mirror_url)
            finally:
                self._posted = []
                self._holding = False
                if from_queue:
                    self.queue.task_done()

    async def _undo_posted(self) -> None:
        for m in reversed(self._posted):
            try:
                if isinstance(m, str):
                    await self.dc.delete_by_url(m)
                else:
                    await m.delete()
            except Exception:
                pass
        self._posted = []

    async def _shot_posts(self, keys: list) -> dict:
        """這幾則紀錄已經拍好的截圖貼文：網址 → 貼文（只讀文字，圖片等摘要那個網址時才下載）。"""
        from enrich.shotworker import success_links
        out: dict = {}
        for k in keys:
            e = self.index.get(k)
            for link in success_links(e.rec.shot if e else None):
                try:
                    m = await self.dc.fetch_by_url(link)
                except Exception:
                    continue
                if m is not None:
                    for u in find_urls(m.content.split("\n", 1)[0]):
                        out.setdefault(u, m)
        return out

    def _text(self, key) -> str:
        e = self.index.get(key)
        return self.text_cache.get(key) or (e.rec.excerpt if e and e.rec.excerpt else "")

    def _own_summaries(self, key, text: str, parts, part_owner, att_keys: dict, n_keys: int) -> str:
        """這筆紀錄自己的摘要：它文字裡的連結、和對應到它的那張圖／檔案。
        一則相簿有很多張圖時，每張圖的關鍵字與重點只看自己的，不能把整則的摘要全塞給每一張（會把別張圖的人事物混進來）。"""
        from ocr import _norm
        ent = self.index.get(key)
        mine = _norm(ent.rec.filename) if ent is not None and ent.rec.filename else ""
        urls = set(find_urls(text))
        out = []
        for (title, summ), (kind, ref) in zip(parts, part_owner):
            if kind == "link":
                ok = ref in urls
            else:
                ok = key in att_keys.get(ref, []) or (bool(mine) and mine == _norm(ref)) or (n_keys == 1 and not att_keys)
            if ok:
                out.append(f"{title}：{summ}")
        return "；".join(out)

    async def process(self, job: EnrichJob) -> None:
        cfg = self.cfg
        keys = [k for k in job.keys if k in self.index]
        if not keys:
            return
        mirror = await self.dc.fetch_by_url(job.mirror_url)
        if mirror is None:
            log.info("鏡像已不存在，略過分析：%s", job.mirror_url)
            return
        if self.ocr_worker is not None:
            await self.ocr_worker.ensure(job.mirror_url)      # 圖片分析一定要有 OCR 當參考：還沒辨識就插隊先辨識、等它做完
        import records
        full = records.mirror_texts([e.rec for e in self.index.by_mirror(job.mirror_url)], mirror.content or "")
        texts = {k: self.text_cache.get(k) or full.get(k) or self._text(k) for k in keys}   # 摘錄只有 200 字，盡量用全文
        parts: list[tuple[str, str]] = []
        part_owner: list[tuple[str, str]] = []        # 和 parts 一一對應：("link", 網址) 或 ("att", 檔名)，之後每筆紀錄只取屬於自己的那幾段
        # 細部進度：連結摘要＋附件＋每則關鍵字，各算一步
        n_urls = len({u for t in texts.values() for u in find_urls(t)}) if cfg.link_summary else 0
        n_atts = sum(1 for a in mirror.attachments if a.size <= MAX_FILE and not is_converted_copy(a.filename))
        activity.begin_item(n_urls + n_atts + (len(keys) if cfg.keywords else 0))

        all_urls: list[str] = []
        for t in texts.values():
            for u in find_urls(t):
                if u not in all_urls:
                    all_urls.append(u)
        shot_posts: dict = {}
        if cfg.link_summary and all_urls and self.shot_worker is not None:
            lane = lanes.get("shot")
            if lane is not None and lane.allowed():          # 截圖車道現在可以跑：先截圖再摘要，截圖當輔助來源
                await self.shot_worker.ensure(job.mirror_url)
            shot_posts = await self._shot_posts(keys)
        img_links: list[str] = []
        if cfg.link_summary:
            caption = " ".join(texts.values())[:300]
            for u in all_urls:                                # 每個網址都摘要；一次只抓一個網頁／一張截圖，用完就丟
                probed = await probe_link(u)
                png = None
                post = shot_posts.get(u)
                if post is not None and post.attachments:
                    try:
                        png = await post.attachments[0].read()
                    except Exception:
                        png = None
                r = await summarize_probed(self.llm, u, probed, embed_preview(mirror, u), png,
                                           caption, describe_image=cfg.image_summary)
                probed, png = None, None
                parts.append((r.title, r.summary))
                part_owner.append(("link", u))
                if r.image is not None:
                    img_links.append(u)
                activity.step()
        shot_posts.clear()

        att_keys: dict[str, list] = {}                              # 附件檔名 → 對應的紀錄 key（相簿裡每張圖各對各的）
        for ent, att in match_attachments(self.index.by_mirror(job.mirror_url), list(mirror.attachments),
                                          mirror.content or ""):
            if att is not None and ent.rec.key in keys:
                att_keys.setdefault(att.filename, []).append(ent.rec.key)
        for a in mirror.attachments:
            name = a.filename
            if a.size > MAX_FILE or is_converted_copy(name):      # HEIC 的 JPEG 副本不重複分析
                continue
            is_img = (a.content_type or "").startswith("image/") or ext_of(name) in MIME
            if is_img and cfg.image_summary:
                data = await a.read()
                own = att_keys.get(name, [])                        # 這張圖對應的紀錄（Discord 會改檔名，用 match_attachments 對）
                caption = " ".join(texts.get(k, "") for k in (own or texts))[:300]
                ocr_txt = " ".join(useful(self.index.get(k).rec.ocr) for k in own if self.index.get(k))
                if ocr_txt:
                    caption = f"{caption}\n{ocr_caption(ocr_txt)}"
                parts.append((f"🖼️ {name}", await summarize_image(self.llm, data, name, caption)))
                part_owner.append(("att", name))
            elif not is_img and cfg.document_summary and (is_supported(name) or ext_of(name) in LEGACY_EXT):
                data = await a.read()
                parts.append((f"📄 {name}", await summarize_document(self.llm, name, data)))
                part_owner.append(("att", name))
            data = b""
            activity.step()

        reply_url = None
        old_replies = {self.index.get(k).rec.reply for k in keys if self.index.get(k) and self.index.get(k).rec.reply}
        if parts:
            lines: list[str] = []
            for title, s in parts:
                lines.append(f"**{title}**")
                lines += [f"> {x}" for x in s.split("\n") if x.strip()]
            from enrich.shotworker import shot_line, success_links
            shot_urls = [u for k in keys for u in success_links(self.index.get(k).rec.shot)]   # 截圖車道先拍好的
            if shot_urls:
                lines.append(shot_line(list(dict.fromkeys(shot_urls))))
            chunks = daily.split_message("\n".join(lines))
            reply = await mirror.reply(chunks[0], mention_author=False, suppress_embeds=True)
            self._posted.append(reply)
            for c in chunks[1:]:
                self._posted.append(await mirror.channel.send(c, suppress_embeds=True))
            reply_url = reply.jump_url
            for old in old_replies - {reply_url}:             # 重新分析（舊的是沒有 OCR 時做的）：新回覆發出後才刪舊的
                await self.dc.delete_by_url(old)
        # 這次分析有沒有拿到 OCR 當參考：沒有 ocr_worker（終端機模式）或圖片都辨識完了才算；之後 OCR 出來會自動補做
        import ipc
        ocr_done = (not ipc.enabled()) if self.ocr_worker is None else not self.ocr_worker.pending_for(job.mirror_url)

        for k in keys:
            kw, gist = [], ""
            ent = self.index.get(k)
            ocr_txt = useful(ent.rec.ocr) if ent else ""
            summaries = self._own_summaries(k, texts[k], parts, part_owner, att_keys, len(keys))
            material = "；".join(x for x in (texts[k], summaries, ocr_txt and f"圖中文字：{ocr_txt[:1500]}") if x)
            if cfg.keywords and material.strip():
                kw, gist = await extract_keywords(self.llm, material, list(self._recent))
                activity.step()
                if self.memory and self.memory.observe(material, kw, gist):
                    asyncio.create_task(self.memory.upgrade(f"自動：累積 {len(self.memory.observations)} 則新分析"))
            if texts[k].strip():
                self._recent.append(texts[k][:200])
            extra = {}
            mine = [u for u in img_links if u in find_urls(texts[k])]
            if mine:
                extra["img"] = " ".join(mine)
            await self.index.update(k, kw="、".join(kw) or None, gist=gist or None,
                                    enriched=True, enriched_ocr=ocr_done, reply=reply_url or next(iter(old_replies), None), **extra)
        if img_links and self.on_link_images is not None:
            self.on_link_images(job.mirror_url)          # 連到圖片的網址 → 交給本機文字辨識
        log.info("已分析 %s（%d 則，回覆 %s，佇列剩 %d）", job.mirror_url.rsplit("/", 1)[-1],
                 len(keys), "有" if reply_url else "無", self.queue.qsize())
