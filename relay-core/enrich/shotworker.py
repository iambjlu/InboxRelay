# relay-core/enrich/shotworker.py
"""連結截圖（獨立車道 shot）：不跟著外部 LLM 排程，有自己的排程設定（SHOT_SCHEDULE／SHOT_START／SHOT_END，預設永遠立即執行）。

- 判斷依據是 #關鍵字庫：紀錄文字裡有網頁網址、但 `shot:` 還是空的 → 排進佇列（積欠的舊連結每次開始都會補拍）。
- 連到圖片／檔案的網址不截圖；拍不到的這一輪重試幾次，下一輪開始時再試。
- 截圖貼到連結預覽頻道，記在紀錄的 shot 欄位；該則鏡像已有 AI 摘要回覆的話，在回覆最後補上「📸 截圖」連結。
- 截圖只在記憶體處理，不寫硬碟。
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque

import activity
from enrich.link import find_urls, probe_link

log = logging.getLogger("shot")

MAX_URLS = 2          # 每則紀錄最多拍幾個網址
SHOT_WAIT_SEC = 240      # AI 分析最多等截圖多久
MAX_TRIES = 2         # 同一則這一輪拍不到幾次就先放棄（下一輪再試）
SHOT_LINE = "📸 截圖："


def shot_line(urls: list[str]) -> str:
    return SHOT_LINE + " ".join(f"[↗{i}](<{u}>)" for i, u in enumerate(urls, 1))


class ShotWorker:
    def __init__(self, cfg, dc, index, text_cache) -> None:
        self.cfg = cfg
        self.dc = dc
        self.index = index
        self.text_cache = text_cache
        self.queue: deque[str] = deque()          # 鏡像連結
        self._queued: set[str] = set()
        self._wake = asyncio.Event()
        self._holding = False
        self._fails: dict = {}                    # 紀錄 → 這一輪拍不到的次數
        self._nonweb: set = set()                 # 確認只有圖片／檔案網址的紀錄（行程存活期間不再檢查）
        self._backoff = 30.0
        self._waiters: dict[str, list] = {}       # 鏡像連結 → 等這則截圖做完的 Future（AI 分析要拿截圖當輔助）

    # ------------------------------------------------------------ 佇列
    def _text(self, e) -> str:
        return self.text_cache.get(e.rec.key) or e.rec.excerpt or ""

    def needs_shot(self, e) -> bool:
        r = e.rec
        return (r.origin != "extra" and not r.shot and r.key not in self._nonweb
                and bool(find_urls(self._text(e))))

    def submit(self, mirror_url: str, front: bool = False) -> None:
        if not mirror_url:
            return
        if mirror_url in self._queued:
            if front and self.queue and self.queue[0] != mirror_url:
                self.queue.remove(mirror_url)
                self.queue.appendleft(mirror_url)
        else:
            self._queued.add(mirror_url)
            (self.queue.appendleft if front else self.queue.append)(mirror_url)
        self._wake.set()

    async def ensure(self, mirror_url: str, timeout: float = SHOT_WAIT_SEC) -> None:
        """AI 分析前呼叫：這則鏡像的連結還沒截圖就插隊先拍，等拍完（或逾時）才回來。"""
        if not any(self.needs_shot(e) for e in self.index.by_mirror(mirror_url)):
            return
        fut = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(mirror_url, []).append(fut)
        self.submit(mirror_url, front=True)
        try:
            await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            log.info("等截圖逾時，先不用截圖做摘要：%s", mirror_url)
        finally:
            ws = self._waiters.get(mirror_url, [])
            if fut in ws:
                ws.remove(fut)
            if not ws:
                self._waiters.pop(mirror_url, None)

    def _release(self, mirror_url: str) -> None:
        for fut in self._waiters.pop(mirror_url, []):
            if not fut.done():
                fut.set_result(None)

    def scan_index(self) -> int:
        self._fails.clear()
        n = 0
        for e in sorted(self.index.entries.values(), key=lambda e: e.msg_id):
            if self.needs_shot(e) and e.rec.mirror not in self._queued:
                self.submit(e.rec.mirror)
                n += 1
        if n:
            log.info("連結截圖：%d 則鏡像有連結還沒截圖，已排入「連結截圖」佇列", n)
        return n

    async def cycle(self) -> None:
        """車道每次開始（排程或立即執行）：重新掃描，把所有積欠、沒截到圖的連結排進佇列。"""
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
            await activity.gate("shot")
            mirror = self.queue.popleft()
            self._queued.discard(mirror)
            self._holding = True
            try:
                if task is None:
                    task = activity.start("連結截圖")
                task.set(done=done, total=done + 1 + len(self.queue))
                if not await self.process(mirror):
                    self.queue.appendleft(mirror)
                    self._queued.add(mirror)
                    log.info("連結截圖暫停 %.0f 秒後重試", self._backoff)
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, 900)
                    continue
                self._backoff = 30.0
                self._release(mirror)
                done += 1
                task.set(done=done)
            except Exception:
                log.exception("連結截圖失敗：%s", mirror)
                self._release(mirror)
            finally:
                self._holding = False

    async def process(self, mirror_url: str) -> bool:
        """回傳 False＝暫時失敗，稍後重試。"""
        from enrich.screenshot import available, post_screenshots
        entries = [e for e in self.index.by_mirror(mirror_url) if self.needs_shot(e)]
        if not entries:
            return True
        if not available():
            return True
        try:
            msg = await self.dc.fetch_by_url(mirror_url)
        except Exception as ex:
            log.warning("讀取鏡像失敗（%s），稍後重試：%s", type(ex).__name__, mirror_url)
            return False
        if msg is None:
            return True
        full = find_urls(getattr(msg, "content", "") or "")
        taken: dict[str, str] = {}                      # 網址 → 截圖貼文連結（同一則鏡像重複的網址只拍一次）
        for e in entries:
            urls = []
            for u in find_urls(self._text(e)):
                u = next((f for f in full if f.startswith(u)), u)       # 摘要被截斷的網址用鏡像裡的完整版
                if u not in urls:
                    urls.append(u)
            web = []
            for u in urls:
                kind = (await probe_link(u))[1]
                if kind not in ("image", "file", "private"):
                    web.append(u)
            if not web:
                self._nonweb.add(e.rec.key)
                continue
            web = web[:MAX_URLS]
            need = [u for u in web if u not in taken]
            if need:
                await post_screenshots(self.dc, self.cfg.ch_screenshot, need, e.rec.date, mirror_url, links=taken)
            links = [taken[u] for u in web if u in taken]
            if not links:
                tries = self._fails.get(e.rec.key, 0) + 1
                self._fails[e.rec.key] = tries
                log.warning("連結截圖失敗（第 %d 次）：TG#%s", tries, e.rec.tg_msg)
                if tries < MAX_TRIES:
                    return False
                continue
            self._fails.pop(e.rec.key, None)
            await self.index.update(e.rec.key, shot=" ".join(links))
            await self._append_to_reply(e.rec.reply, links)
            log.info("📸 已補截圖：TG#%s（%d 張）", e.rec.tg_msg, len(links))
        return True

    async def _append_to_reply(self, reply_url: str | None, links: list[str]) -> None:
        if not reply_url:
            return
        try:
            rm = await self.dc.fetch_by_url(reply_url)
            if rm is None or SHOT_LINE in rm.content:
                return
            new = f"{rm.content}\n{shot_line(links)}"
            if len(new) <= 2000:
                await rm.edit(content=new)
        except Exception:
            log.warning("在 AI 回覆補上截圖連結失敗：%s", reply_url, exc_info=True)
