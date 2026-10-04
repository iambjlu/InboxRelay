# relay-core/ocr.py
"""圖片文字辨識（第 7.3.2 節）：由選單列 App 用 Vision（Apple Neural Engine）辨識，核心只負責排隊與存結果。

- 判斷依據是 #關鍵字庫：圖片紀錄沒有 `ocr:` 欄位＝還沒辨識 → 排進佇列（舊圖片自動補做）。
- 文字存在 #關鍵字庫 裡這張圖自己的 OCR 訊息（OCR|tg_chat|tg_msg，一張圖一則、最多塞滿一則），
  主紀錄的 `ocr:` 欄位寫「見 OCR 訊息」；沒有字寫「（無文字）」，拿不到圖寫「（無法取得圖片）」；每次開始辨識（排程或立即執行）都會把這些以及未完成的圖片全部重做。
- 搜尋、關鍵字、每日總結都會用到圖中文字。
- 辨識出文字的那天如果「已經有每日總結」，會標記「需要重新總結」（RECAP_DIRTY），
  下一次外部 LLM 工作時重寫那天的總結，並連帶更新月／年總結。還沒寫總結的日子不用標，寫的時候自然會用到。
- 看起來像信用卡號的數字（通過 Luhn 檢查）只留末四碼。
- 什麼時候做由「本機 ANE」車道決定（lanes.py）；App 沒開時這條車道顯示「需要 App」。
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
from collections import deque

import activity
import lanes
import records

log = logging.getLogger("ocr")

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff")
NO_TEXT, NO_IMAGE = records.OCR_MARKERS     # 「（無文字）」「（無法取得圖片）」
MARKERS = records.OCR_MARKERS + (records.OCR_REF,)
OCR_MAX = records.OCR_ROOM   # 一張圖一則 OCR 訊息，最多塞滿一則（約 1,960 字）
LEGACY_CUT = 900        # 舊版只存約 1000 字並以「…」結尾 → 重新辨識取得全文
TIMEOUT = 120
MAX_TRIES = 3          # 同一張圖辨識失敗幾次才標「無法取得圖片」
PACE_SEC = 0.4          # 每張圖之間稍微停一下，避免 Discord 圖片伺服器限流
WRITE_AHEAD = 30        # 辨識可以領先 Discord 寫入幾張（寫入慢、辨識快；超過就等寫入追上來）
WAIT_SEC = 1800         # AI 分析最多等這則的 OCR 多久（安全上限）
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def is_image_record(rec) -> bool:
    if rec.origin == "extra":
        return False
    name = (rec.filename or "").lower()
    if rec.type == "image":
        return True
    if rec.type == "album":                  # 相簿裡也可能有影片：有檔名就看副檔名
        return not name or name.endswith(IMAGE_EXT)
    return name.endswith(IMAGE_EXT)


def is_link_image_record(rec) -> bool:
    """文字裡有「連到圖片」的網址（分析時發現的），而且自己不是圖片紀錄。"""
    return bool(rec.img) and rec.origin != "extra" and not is_image_record(rec)


def needs_ocr(rec) -> bool:
    if is_link_image_record(rec):
        return not rec.ocr
    if not is_image_record(rec):
        return False
    if not rec.ocr:
        return True
    return len(rec.ocr) >= LEGACY_CUT and rec.ocr.endswith("…")      # 舊版被截斷的 → 重做取得全文


def useful(ocr: str | None) -> str:
    """給搜尋／關鍵字／總結用的圖中文字（標記值當作沒有）。"""
    return "" if not ocr or ocr in MARKERS else ocr


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def clean(text: str) -> str:
    def mask(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return "＊＊＊＊" + digits[-4:]
        return m.group(0)
    lines = [" ".join(ln.split()) for ln in text.splitlines()]
    out = "\n".join(ln for ln in lines if ln)
    out = _CARD.sub(mask, out)
    if len(re.sub(r"\s", "", out)) < 3:
        return NO_TEXT
    return out if len(out) <= OCR_MAX else out[:OCR_MAX]


def _norm(name: str | None) -> str:
    """比對用檔名：Discord 會改檔名（空白、@、中文等換成底線或拿掉、加 SPOILER_），只留英數字。"""
    n = (name or "").lower()
    if n.startswith("spoiler_"):
        n = n[8:]
    stem, dot, ext = n.rpartition(".")
    stem = re.sub(r"[^0-9a-z]", "", stem if dot else n)
    return f"{stem}.{re.sub(r'[^0-9a-z]', '', ext)}" if stem and dot else stem


def _missing(content: str) -> set[str]:
    """鏡像內文裡「📎 檔名（未匯出／過大未搬移…）」的檔名＝本來就沒有圖。"""
    out = set()
    for ln in (content or "").split("\n"):
        if ln.startswith("📎 "):
            out.add(_norm(ln[2:].split("（", 1)[0].strip()))
    out.discard("")
    return out


def match_attachments(entries: list, attachments: list, content: str = "") -> list[tuple]:
    """紀錄 ↔ 鏡像附件：先比檔名（原樣→去掉特殊字元），剩下的扣掉確定沒搬的，數量對得上就依順序對。"""
    from enrich.image import is_converted_copy
    imgs = [a for a in attachments if not is_converted_copy(a.filename)
            and ((a.content_type or "").startswith("image/") or a.filename.lower().endswith(IMAGE_EXT))]
    image_entries = [e for e in sorted(entries, key=lambda e: e.rec.tg_msg) if is_image_record(e.rec)]
    got: dict = {}
    free = list(imgs)
    for same in (lambda a, b: a == b, lambda a, b: bool(_norm(a)) and _norm(a) == _norm(b)):
        for e in image_entries:
            if e.rec.key in got or not e.rec.filename:
                continue
            for a in free:
                if same(e.rec.filename, a.filename):
                    got[e.rec.key] = a
                    free.remove(a)
                    break
    gone = _missing(content)
    rest = [e for e in image_entries if e.rec.key not in got and _norm(e.rec.filename) not in gone]
    if rest and len(rest) == len(free):
        for e, a in zip(rest, free):
            got[e.rec.key] = a
    return [(e, got.get(e.rec.key)) for e in image_entries]


class OcrWorker:
    def __init__(self, dc, index, send) -> None:
        self.dc = dc
        self.index = index
        self.send = send                      # send(dict)：寫一行 JSON 給 App
        self.queue: deque[str] = deque()      # 鏡像連結
        self.rewrite: deque = deque()         # 舊格式（⏎）紀錄：不用重新辨識，只重寫成新格式
        self._queued: set[str] = set()
        self._wake = asyncio.Event()
        self._futures: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._holding = False
        self.done_total = 0
        self.has_daily = None                 # callable(day) -> bool：那天是否已有每日總結
        self.mark_days = None                 # async callable(list[day])：標記需要重新總結
        self._dirty: set[str] = set()
        self._fails: dict = {}                 # 紀錄 → 辨識失敗次數
        self._retry_keys: set = set()          # 標過「無法取得圖片」但要再試一次的紀錄
        self._backoff = 30.0
        self._waiters: dict[str, list] = {}    # 鏡像連結 → 等這則辨識完的 Future（AI 分析要拿 OCR 當參考）
        self._pending: set = set()             # 寫進 #關鍵字庫 還沒完成的工作（辨識不等寫入）
        self._write_slots = asyncio.Semaphore(WRITE_AHEAD)
        self.on_text = None                    # callable(entry)：辨識出文字、而且這則已經分析過 → 通知重新分析

    # ------------------------------------------------------------ 佇列
    def submit(self, mirror_url: str, front: bool = False) -> None:
        if not mirror_url:
            return
        if mirror_url in self._queued:
            if front and self.queue and self.queue[0] != mirror_url:
                try:
                    self.queue.remove(mirror_url)
                except ValueError:
                    return
                self.queue.appendleft(mirror_url)
            return
        self._queued.add(mirror_url)
        (self.queue.appendleft if front else self.queue.append)(mirror_url)
        self._wake.set()

    def pending_for(self, mirror_url: str) -> bool:
        """這則鏡像還有圖片的文字沒辨識完（含要重試的）。"""
        return any(needs_ocr(e.rec) or e.rec.key in self._retry_keys for e in self.index.by_mirror(mirror_url)
                   if is_image_record(e.rec) or is_link_image_record(e.rec))

    async def ensure(self, mirror_url: str, timeout: float = WAIT_SEC) -> None:
        """AI 分析前呼叫：這則的圖片還沒辨識完就插隊先辨識，等辨識（含寫入）完成才回來。
        本機 ANE 車道沒在跑（暫停、還沒到排程、App 還沒連上）就不等，直接分析。"""
        import lanes
        lane = lanes.get("ane")
        if lane is None or lane.state != "running" or not self.pending_for(mirror_url):
            return
        fut = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(mirror_url, []).append(fut)
        self.submit(mirror_url, front=True)
        try:
            await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            log.info("等圖片文字辨識逾時，先不用 OCR 做分析：%s", mirror_url)
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

    async def _write(self, key, **changes) -> None:
        """把辨識結果寫進 #關鍵字庫：丟到背景去寫，辨識不用等 Discord（領先超過 WRITE_AHEAD 張才等）。"""
        await self._write_slots.acquire()

        async def job() -> None:
            try:
                await self.index.update(key, **changes)
                e = self.index.get(key)
                if e is not None and self.on_text is not None and useful(changes.get("ocr")) \
                        and e.rec.enriched and not e.rec.enriched_ocr:
                    self.on_text(e)                       # 先前分析時沒有 OCR → 補做
            except Exception:
                log.exception("寫入圖中文字失敗：TG#%s", key[1])
            finally:
                self._write_slots.release()

        t = asyncio.get_running_loop().create_task(job())
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    async def flush(self, mirror_url: str | None = None) -> None:
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def _clear_stale(self, entries) -> None:
        for e in entries:
            try:
                await self.index.update(e.rec.key, ocr=None)
            except Exception:
                log.warning("清除影片的圖中文字欄位失敗：TG#%s", e.rec.tg_msg, exc_info=True)
        log.info("圖片文字辨識：清掉 %d 筆相簿影片誤標的 ocr 欄位", len(entries))

    def submit_entries(self, entries) -> int:
        n = 0
        for e in sorted(entries, key=lambda e: e.msg_id):
            if needs_ocr(e.rec):
                self.submit(e.rec.mirror)
                n += 1
        return n

    def scan_index(self) -> int:
        entries = list(self.index.entries.values())
        n = self.submit_entries(entries)
        stale = [e for e in entries if e.rec.type == "album" and not is_image_record(e.rec) and e.rec.ocr]
        if stale:                                  # 舊版把相簿裡的影片也當成圖片 → 誤標「無法取得圖片」，清掉
            try:
                asyncio.get_running_loop().create_task(self._clear_stale(stale))
            except RuntimeError:
                pass
        retry = [e for e in entries if e.rec.ocr == NO_IMAGE and (is_image_record(e.rec) or is_link_image_record(e.rec))]
        for e in sorted(retry, key=lambda e: e.msg_id):      # 先前「無法取得圖片」的每次都重試
            self._retry_keys.add(e.rec.key)
            self._fails.pop(e.rec.key, None)
            self.submit(e.rec.mirror)
        if retry:
            log.info("圖片文字辨識：%d 張先前無法取得或辨識失敗的圖片會再試一次", len(retry))
        n += len(retry)
        for e in entries:
            if getattr(e, "legacy", False) and not needs_ocr(e.rec):
                self.rewrite.append(e.rec.key)
        if self.rewrite:
            log.info("圖中文字：%d 筆舊格式紀錄會改寫成新格式（去掉 ⏎）", len(self.rewrite))
            self._wake.set()
        if n:
            log.info("圖片文字辨識：%d 張圖片還沒辨識，已排入「本機 ANE」佇列", n)
        return n

    async def cycle(self) -> None:
        """「本機 ANE」車道每次開始（排程或立即執行）時：重新掃描，把未辨識、未完成、無法取得的圖片全部排進佇列。"""
        self.scan_index()

    @property
    def backlog(self) -> int:
        return len(self.queue) + len(self.rewrite) + (1 if self._holding else 0) + len(self._pending)

    @property
    def drained(self) -> bool:
        return not self.queue and not self.rewrite and not self._holding and not self._pending

    def on_result(self, cmd: dict) -> None:
        fut = self._futures.pop(int(cmd.get("id", 0)), None)
        if fut is not None and not fut.done():
            fut.set_result(cmd)

    # ------------------------------------------------------------ 執行
    async def run(self) -> None:
        task, done = None, 0
        while True:
            if self._dirty and (not self.queue or len(self._dirty) >= 20):
                await self._flush_dirty()
            if not self.queue and self.rewrite:
                await activity.gate("ane")
                key = self.rewrite.popleft()
                self._holding = True
                try:
                    e = self.index.get(key)
                    if e is not None and getattr(e, "legacy", False):
                        await self.index.update(key)          # 用新格式重寫（含續頁）
                except Exception:
                    log.exception("改寫圖中文字紀錄失敗")
                finally:
                    self._holding = False
                continue
            if not self.queue:
                if task is not None:
                    activity.finish(task, f"{done:,} 則鏡像")
                    task, done = None, 0
                self._wake.clear()
                await self._wake.wait()
                continue
            await activity.gate("ane")
            mirror = self.queue.popleft()
            self._queued.discard(mirror)
            self._holding = True
            try:
                if task is None:
                    task = activity.start("圖片文字辨識（本機 ANE）")
                task.set(done=done, total=done + 1 + len(self.queue))
                ok = await self.process(mirror)
                if ok:
                    await self.flush()                       # 等寫入追上來，AI 分析才讀得到這則的 OCR
                    self._release(mirror)
                if not ok:                       # 暫時失敗（App 沒回應、下載被限流…）：放回去，等一下再試
                    self.queue.appendleft(mirror)
                    self._queued.add(mirror)
                    log.info("圖片文字辨識暫停 %.0f 秒後重試", self._backoff)
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, 900)
                    continue
                self._backoff = 30.0
                done += 1
                task.set(done=done)
            except Exception:
                log.exception("圖片文字辨識失敗：%s", mirror)
                self._release(mirror)
            finally:
                self._holding = False

    async def _flush_dirty(self) -> None:
        days, self._dirty = sorted(self._dirty), set()
        if self.mark_days is None or not days:
            return
        try:
            await self.mark_days(days)
            log.info("🔤 %d 天有新的圖中文字，已標記重新總結（%s）", len(days), "、".join(days[:5]) + ("…" if len(days) > 5 else ""))
        except Exception:
            log.exception("標記重新總結失敗")
            self._dirty |= set(days)

    async def _ask(self, url: str, name: str) -> dict | None:
        await asyncio.sleep(PACE_SEC)
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._futures[rid] = fut
        self.send({"type": "ocr_request", "id": rid, "url": url, "name": name})
        try:
            return await asyncio.wait_for(fut, timeout=TIMEOUT)
        except asyncio.TimeoutError:
            self._futures.pop(rid, None)
            return None

    async def _link_images(self, e, mirror_url: str) -> bool:
        """辨識這則紀錄文字裡連到的圖片（可能好幾張），文字合併存進 ocr。回傳 False＝稍後再試。"""
        texts, got_any = [], False
        for url in (e.rec.img or "").split():
            res = await self._ask(url, url.rsplit("/", 1)[-1][:80])
            if res is None:
                log.warning("App 沒有回傳文字辨識結果（%s），稍後再試", url)
                return False
            if res.get("error"):
                tries = self._fails.get((e.rec.key, url), 0) + 1
                self._fails[(e.rec.key, url)] = tries
                log.warning("網址圖片%s失敗（第 %d 次）：%s", "下載" if res.get("retry") else "辨識", tries, res["error"])
                if tries < MAX_TRIES:            # 外部網站可能暫時連不上 → 稍後再試，試滿才放棄
                    if res.get("retry"):
                        wait = float(res.get("retry_after") or 0)
                        if wait > self._backoff:
                            self._backoff = min(wait, 900)
                    return False
                continue
            got_any = True
            t = clean(str(res.get("text", "")))
            if t not in MARKERS:
                texts.append(t)
        self._retry_keys.discard(e.rec.key)
        for url in (e.rec.img or "").split():
            self._fails.pop((e.rec.key, url), None)
        text = "\n".join(texts) if texts else (NO_TEXT if got_any else NO_IMAGE)
        await self._write(e.rec.key, ocr=text[:OCR_MAX])
        self.done_total += 1
        day = e.rec.date[:10]
        if texts and self.has_daily is not None and self.has_daily(day):
            self._dirty.add(day)
        if texts:
            log.info("🔤 網址圖片：%d 字（TG#%s）", len(text), e.rec.tg_msg)
        return True

    async def process(self, mirror_url: str) -> bool:
        def wanted(e) -> bool:
            return needs_ocr(e.rec) or e.rec.key in self._retry_keys

        entries = [e for e in self.index.by_mirror(mirror_url) if wanted(e)]
        if not entries:
            return True
        for e in entries:                        # 連到圖片的網址：App 直接下載那個網址辨識
            if is_link_image_record(e.rec) and not await self._link_images(e, mirror_url):
                return False
        if not any(is_image_record(e.rec) for e in entries):
            return True
        try:
            msg = await self.dc.fetch_by_url(mirror_url)
        except Exception as ex:                  # Discord 暫時有問題 → 稍後重試（不要記成拿不到圖）
            log.warning("讀取鏡像失敗（%s），稍後重試：%s", type(ex).__name__, mirror_url)
            return False
        all_entries = self.index.by_mirror(mirror_url)
        atts = msg.attachments if msg is not None else []
        pairs = match_attachments(all_entries, atts, getattr(msg, "content", "") or "")
        for e, att in pairs:
            if not wanted(e):
                continue
            if att is None:                      # 鏡像裡真的沒有這張圖（例如當初太大沒搬）
                self._retry_keys.discard(e.rec.key)
                log.warning("%s：TG#%s %s", "鏡像已刪除" if msg is None else "對不到鏡像附件", e.rec.tg_msg, e.rec.filename)
                if e.rec.ocr != NO_IMAGE:
                    await self._write(e.rec.key, ocr=NO_IMAGE)
                continue
            await asyncio.sleep(PACE_SEC)
            rid = next(self._ids)
            fut = asyncio.get_running_loop().create_future()
            self._futures[rid] = fut
            self.send({"type": "ocr_request", "id": rid, "url": att.url, "name": att.filename})
            try:
                res = await asyncio.wait_for(fut, timeout=TIMEOUT)
            except asyncio.TimeoutError:
                self._futures.pop(rid, None)
                log.warning("App 沒有回傳文字辨識結果（%s），稍後再試", att.filename)
                return False
            if res.get("error") and res.get("retry"):
                wait = float(res.get("retry_after") or 0)
                if wait > self._backoff:
                    self._backoff = min(wait, 900)
                log.warning("圖片下載暫時失敗（%s）：%s，稍後重試", att.filename, res["error"])
                return False
            if res.get("error"):
                tries = self._fails.get(e.rec.key, 0) + 1
                self._fails[e.rec.key] = tries
                log.warning("文字辨識失敗（%s，第 %d 次）：%s", att.filename, tries, res["error"])
                if tries < MAX_TRIES:            # 辨識引擎偶爾會暫時出錯 → 等一下再試，試滿才放棄
                    return False
                text = NO_IMAGE
            self._retry_keys.discard(e.rec.key)
            self._fails.pop(e.rec.key, None)
            if not res.get("error"):
                text = clean(str(res.get("text", "")))
            await self._write(e.rec.key, ocr=text)
            self.done_total += 1
            day = e.rec.date[:10]
            if text not in MARKERS and self.has_daily is not None and self.has_daily(day):
                self._dirty.add(day)
            if text not in MARKERS:
                log.info("🔤 %s：%d 字（%s ms）", att.filename, len(text), res.get("ms", "?"))
        return True
