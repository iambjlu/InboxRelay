# relay-core/enrich/audioworker.py
"""音檔車道：語音轉文字＋摘要，自己的運作時間（lanes "audio"，AUDIO_SCHEDULE／START／END），和一般 AI 分析分開。

- 一般 AI 分析（Enricher）不碰音檔；分析完一則鏡像後呼叫 `consider(mirror)`，有沒處理過的音檔就排進這裡。
- `rec.audio` 記錄這個音檔的摘要訊息連結（「-」＝不用再處理）；啟動與每次車道開始時 `scan_index()` 把沒處理的都排進來。
- 總結（Recapper）會等這個佇列做完（`drained`）。
- 一則鏡像一則摘要訊息：每個音檔一段 `🎙️ 檔名` 條列，逐字稿存成附件，附「詳細整理」按鈕；鏡像加一行 `↗ 錄音` 小箭頭。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import discord

import activity
import lanes
from enrich import audio, summaryhome
from enrich.audiobutton import NotesView
from enrich.keywords import extract_keywords
from llm import is_offline_error
from ocr import _norm
from recap import daily

log = logging.getLogger("audioworker")

MAX_AUDIO = 20 * 1024 * 1024
MAX_TRIES = 3
NOMODEL_WAIT = 600            # 伺服器沒有語音模型：這麼久之後下一輪再看
ARROW_LABEL = "錄音"


def pair_audio(entries: list, attachments: list) -> list[tuple]:
    """音檔紀錄 ↔ 鏡像附件：先比檔名（原樣→去掉特殊字元），剩下的數量一樣就依順序對。"""
    recs = sorted((e for e in entries if audio.is_audio(e.rec.filename or "")), key=lambda e: e.rec.tg_msg)
    free = [a for a in attachments if audio.is_audio(a.filename, getattr(a, "content_type", None))]
    got: dict = {}
    for same in (lambda a, b: a == b, lambda a, b: bool(_norm(a)) and _norm(a) == _norm(b)):
        for e in recs:
            if e.rec.key in got:
                continue
            for a in free:
                if same(e.rec.filename or "", a.filename):
                    got[e.rec.key] = a
                    free.remove(a)
                    break
    rest = [e for e in recs if e.rec.key not in got]
    if rest and len(rest) == len(free):
        got.update({e.rec.key: a for e, a in zip(rest, free)})
    return [(e, got.get(e.rec.key)) for e in recs]


class AudioWorker:
    def __init__(self, relayer, llm) -> None:
        self.r, self.llm = relayer, llm
        self.cfg, self.dc, self.index = relayer.cfg, relayer.dc, relayer.index
        self.queue: deque[str] = deque()
        self._queued: set[str] = set()
        self._wake = asyncio.Event()
        self._holding = False
        self._fails: dict[str, int] = {}
        self._nomodel_until = 0.0

    # ------------------------------------------------------------ 佇列
    @staticmethod
    def needs(e) -> bool:
        r = e.rec
        return (r.origin != "extra" and not r.audio and r.type in ("file", "voice")
                and audio.is_audio(r.filename or ""))

    def submit(self, mirror_url: str, front: bool = False) -> None:
        if not mirror_url or mirror_url in self._queued:
            return
        self._queued.add(mirror_url)
        (self.queue.appendleft if front else self.queue.append)(mirror_url)
        self._wake.set()

    def consider(self, mirror_url: str) -> None:
        """Enricher 分析完一則鏡像後呼叫：裡面有沒處理的音檔就排進來。"""
        if time.monotonic() >= self._nomodel_until and any(self.needs(e) for e in self.index.by_mirror(mirror_url)):
            self.submit(mirror_url)

    def scan_index(self) -> int:
        n = 0
        for e in sorted(self.index.entries.values(), key=lambda e: e.msg_id):
            if self.needs(e) and e.rec.mirror not in self._queued:
                self.submit(e.rec.mirror)
                n += 1
        if n:
            log.info("音檔：%d 則鏡像有音檔還沒處理，已排入「音檔」佇列", n)
        return n

    async def cycle(self) -> None:
        self._nomodel_until = 0.0
        self.scan_index()

    @property
    def backlog(self) -> int:
        return len(self.queue) + (1 if self._holding else 0)

    @property
    def drained(self) -> bool:
        return not self.queue and not self._holding

    # ------------------------------------------------------------ 執行
    async def run(self) -> None:
        task, done = None, 0
        while True:
            if not self.queue:
                if task is not None:
                    activity.finish(task, f"{done:,} 則鏡像")
                    task, done = None, 0
                self._wake.clear()
                await self._wake.wait()
                continue
            await activity.gate("audio")
            url = self.queue.popleft()
            self._queued.discard(url)
            self._holding = True
            try:
                if task is None:
                    task = activity.start("音檔轉文字與摘要")
                task.set(done=done, total=done + 1 + len(self.queue))
                task.set(detail=url.rsplit("/", 1)[-1])
                activity.bind(task)
                result = await self.process(url)
                if result == "nomodel":
                    self._nomodel_until = time.monotonic() + NOMODEL_WAIT
                    self.queue.clear()
                    self._queued.clear()
                done += 1
                task.set(done=done)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                if is_offline_error(ex):
                    self.queue.appendleft(url)
                    self._queued.add(url)
                    lane = lanes.get("audio")
                    if lane is not None:
                        lane.go_offline(type(ex).__name__)
                else:
                    n = self._fails[url] = self._fails.get(url, 0) + 1
                    log.exception("音檔分析失敗（第 %d 次）：%s", n, url)
                    if n >= MAX_TRIES:
                        await self._give_up(url)
            finally:
                self._holding = False

    async def _give_up(self, url: str) -> None:
        for e in self.index.by_mirror(url):
            if self.needs(e):
                await self.index.update(e.rec.key, audio="-")

    async def process(self, url: str) -> str:
        """處理一則鏡像裡所有還沒處理的音檔。回傳 "ok"／"nomodel"。"""
        mirror = await self.dc.fetch_by_url(url)
        ents = self.index.by_mirror(url)
        if mirror is None:
            await self._give_up(url)
            return "ok"
        todo = {e.rec.key for e in ents if self.needs(e)}
        pairs = [(e, a) for e, a in pair_audio(ents, list(mirror.attachments)) if e.rec.key in todo]
        results = []                                   # (entry, 檔名, AudioResult)
        for e, a in pairs:
            if a.size > MAX_AUDIO:
                await self.index.update(e.rec.key, audio="-")
                continue
            data = await a.read()
            res = await audio.analyze_audio(self.llm, self.cfg, a.filename, data)
            data = b""
            if res.kind == "nomodel":
                log.warning("音檔：伺服器沒有語音轉文字模型，%d 秒後下一輪再看", NOMODEL_WAIT)
                return "nomodel"
            results.append((e, a.filename, res))
        if not results:
            return "ok"
        lines, files = [], []
        for _, name, res in results:
            lines.append(f"**🎙️ {name}**")
            lines += [f"> {x}" for x in res.summary.split("\n") if x.strip()]
            if res.transcript:
                files.append((audio.transcript_name(name), res.transcript.encode()))
        view = NotesView() if files else None
        if summaryhome.can_move(self.dc, self.cfg, mirror):
            msgs = await summaryhome.post(self.dc, self.cfg, mirror, "\n".join(lines), files, view, label=ARROW_LABEL)
            link = msgs[0].jump_url
        else:
            kw = {}
            if files:
                import io
                kw = {"files": [discord.File(io.BytesIO(b), filename=n) for n, b in files], "view": view}
            chunks = daily.split_message("\n".join(lines))
            reply = await mirror.reply(chunks[0], mention_author=False, suppress_embeds=True, **kw)
            for c in chunks[1:]:
                await mirror.channel.send(c, suppress_embeds=True)
            link = reply.jump_url
        for e, name, res in results:
            changes = {"audio": link}
            if res.kind == "talk" and self.cfg.keywords:
                material = "；".join(x for x in (e.rec.excerpt, f"錄音「{name}」的摘要：{res.summary}") if x)
                kw_, gist = await extract_keywords(self.llm, material, [])
                changes.update(kw="、".join(kw_) or e.rec.kw, gist=gist or e.rec.gist)
            await self.index.update(e.rec.key, **changes)
        if self.r.dirty is not None:
            await self.r.dirty.mark(results[0][0].rec.date[:10])
        log.info("已處理音檔 %s（%d 個）", url.rsplit("/", 1)[-1], len(results))
        return "ok"
