# relay-core/enrich/shotworker.py
"""連結截圖（獨立車道 shot）：不跟著外部 LLM 排程，有自己的排程設定（SHOT_SCHEDULE／SHOT_START／SHOT_END，預設永遠立即執行）。

- 一則訊息裡的每個網址都處理：網頁 → 截圖貼到連結預覽頻道；拍不到（網域不存在、逾時…）→ 改貼一則「📸 無法截圖」通知；
  連到圖片／檔案、內網被擋的網址不截圖。
- 紀錄的 shot 欄位依網址順序每個網址一個記號：截圖貼文連結／「!」＋通知貼文連結／「-」（不需要截圖）。
  記號比網址少＝還有沒處理的 → 排進佇列（積欠的舊連結每次車道開始都會補）。
- 網址從完整原文找：實時訊息用記憶體裡的原文，其他從鏡像訊息全文切出（摘錄只有 200 字，後面的連結會漏掉）。
- 該則鏡像已有 AI 摘要回覆的話，回覆最後的「📸 截圖」連結會更新成全部成功的截圖。
- 截圖只在記憶體處理，不寫硬碟。
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque

import activity
import records
from enrich.link import find_urls, probe_link

log = logging.getLogger("shot")

SHOT_WAIT_SEC = 240      # AI 分析最多等截圖多久
SHOT_LINE = "📸 截圖："
SKIP = "-"               # 這個網址不需要截圖（圖片／檔案／內網）；沒有網址的長訊息也記一個，避免每輪重讀鏡像


def shot_line(urls: list[str]) -> str:
    return SHOT_LINE + " ".join(f"[↗{i}](<{u}>)" for i, u in enumerate(urls, 1))


def success_links(shot: str | None) -> list[str]:
    """shot 欄位裡成功的截圖貼文連結。"""
    from enrich.screenshot import FAIL_MARK
    return [t for t in (shot or "").split() if t != SKIP and not t.startswith(FAIL_MARK)]


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
        self._backoff = 30.0
        self.on_drained = None                    # callable()：佇列清空時通知（外部 LLM 車道等截圖做完）
        self._waiters: dict[str, list] = {}       # 鏡像連結 → 等這則截圖做完的 Future（AI 分析要拿截圖當輔助）

    # ------------------------------------------------------------ 佇列
    def needs_shot(self, e) -> bool:
        """不讀 Discord 的快速判斷：記號比網址少＝還有沒處理的網址。"""
        r = e.rec
        if r.origin == "extra":
            return False
        tokens = (r.shot or "").split()
        text = self.text_cache.get(r.key)
        if text is not None:
            return len(tokens) < len(find_urls(text))
        ex = r.excerpt or ""
        n = len(find_urls(ex))
        if ex.endswith("…"):                      # 摘錄被截斷：後面可能還有網址，沒處理過就讀鏡像確認
            return not tokens or len(tokens) < n
        return len(tokens) < n

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
        n = 0
        for e in sorted(self.index.entries.values(), key=lambda e: e.msg_id):
            if self.needs_shot(e) and e.rec.mirror not in self._queued:
                self.submit(e.rec.mirror)
                n += 1
        if n:
            log.info("連結截圖：%d 則鏡像有連結還沒處理，已排入「連結截圖」佇列", n)
        return n

    async def cycle(self) -> None:
        """車道每次開始（排程或立即執行）：重新掃描，把所有積欠、沒處理到的連結排進佇列。"""
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
                if self.on_drained is not None:
                    self.on_drained()
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

    async def _covered(self, tokens: list[str]) -> dict[str, str]:
        """舊紀錄已經有的貼文：網址 → 記號（讀貼文第一行的網址），避免重拍。"""
        out: dict[str, str] = {}
        for t in tokens:
            if t == SKIP:
                continue
            try:
                m = await self.dc.fetch_by_url(t.lstrip("!"))
            except Exception:
                continue
            if m is None:
                continue
            for u in find_urls(m.content.split("\n", 1)[0]):
                out.setdefault(u, t)
        return out

    async def process(self, mirror_url: str) -> bool:
        """處理這則鏡像所有紀錄裡還沒處理的網址。回傳 False＝暫時失敗（讀不到鏡像），稍後重試。"""
        from enrich.screenshot import FAIL_MARK, available, post_one
        entries = [e for e in self.index.by_mirror(mirror_url) if self.needs_shot(e)]
        if not entries or not available():
            return True
        try:
            msg = await self.dc.fetch_by_url(mirror_url)
        except Exception as ex:
            log.warning("讀取鏡像失敗（%s），稍後重試：%s", type(ex).__name__, mirror_url)
            return False
        if msg is None:
            return True
        full = records.mirror_texts([x.rec for x in self.index.by_mirror(mirror_url)], getattr(msg, "content", "") or "")
        for e in entries:
            key = e.rec.key
            text = self.text_cache.get(key) or full.get(key) or e.rec.excerpt or ""
            urls = find_urls(text)
            old = (e.rec.shot or "").split()
            covered = await self._covered(old) if old else {}
            tokens: list[str] = []
            ok_links: list[str] = []
            for u in urls:
                if u in covered:
                    tokens.append(covered[u])
                    if not covered[u].startswith(FAIL_MARK):
                        ok_links.append(covered[u])
                    continue
                kind = (await probe_link(u))[1]
                if kind in ("image", "file", "private"):
                    tokens.append(SKIP)
                    continue
                link, ok = await post_one(self.dc, self.cfg.ch_screenshot, u, e.rec.date, mirror_url)
                tokens.append(link if ok else FAIL_MARK + link)
                if ok:
                    ok_links.append(link)
            extras = [t for t in old if t not in tokens and t != SKIP]      # 對不上網址的舊貼文：留著，刪除同步才刪得到
            new = " ".join(tokens + extras) or SKIP
            if new != (e.rec.shot or ""):
                await self.index.update(key, shot=new)
            if ok_links:
                await self._update_reply(e.rec.reply, ok_links)
            log.info("📸 連結截圖：TG#%s 共 %d 個網址（成功 %d）", e.rec.tg_msg, len(urls), len(ok_links))
        return True

    async def _update_reply(self, reply_url: str | None, links: list[str]) -> None:
        """AI 回覆最後的「📸 截圖」那行換成目前全部成功的截圖。"""
        if not reply_url:
            return
        try:
            rm = await self.dc.fetch_by_url(reply_url)
            if rm is None:
                return
            lines = [ln for ln in rm.content.split("\n") if not ln.startswith(SHOT_LINE)]
            new = "\n".join(lines + [shot_line(links)])
            if new != rm.content and len(new) <= 2000:
                await rm.edit(content=new)
        except Exception:
            log.warning("在 AI 回覆更新截圖連結失敗：%s", reply_url, exc_info=True)
