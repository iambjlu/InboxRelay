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
import re
from collections import deque
from datetime import datetime, timezone

import activity
import records
from enrich.link import find_urls, probe_kind
from enrich.screenshot import NetworkDown

log = logging.getLogger("shot")

SHOT_WAIT_SEC = 240      # AI 分析最多等截圖多久
SHOT_LINE = "📸 截圖："
SKIP = "-"               # 這個網址不能截圖（zip／影片等無法預覽的檔案、內網）；沒有網址的長訊息也記一個，避免每輪重讀鏡像
RETRY_MAX = 3            # 失敗的網址最多試幾次（含第一次），之後記成最終失敗
RETRY_AFTER_SEC = 6 * 3600   # 失敗後隔多久才再試（算從失敗通知發出的時間）
RETRY_FLAG = "shot-retry-v1"
ARROW_FLAG = "shot-arrows-v1"
BUTTON_FLAG = "shot-buttons-v1"
SKIP_FLAG = "shot-skip-v3"
RETRY2_FLAG = "shot-retry-v2"
ARROW_PREFIX = "-# [↗ 截圖]("     # 鏡像裡「含連結的那行」下面的小字箭頭行，點了跳到截圖貼文


def add_arrows(content: str, url_links: dict[str, str]) -> str:
    """在鏡像訊息每個有截圖的連結那一行下面加一行小字「↗ 截圖」（先清掉舊的箭頭行，重複執行結果一樣）。"""
    out: list[str] = []
    in_code = False
    for ln in content.split("\n"):
        if ln.startswith(ARROW_PREFIX):
            continue
        out.append(ln)
        if ln.strip().startswith("```"):
            in_code = not in_code
        if in_code or ln.startswith(("#", "-#", "📎", "```")):
            continue
        links: list[str] = []
        for u in find_urls(ln):
            link = url_links.get(u)
            if link and link not in links:
                links.append(link)
        if links:
            out.append("-# " + " ".join(f"[↗ 截圖](<{l}>)" for l in links))
    return "\n".join(out)


def parse_token(t: str) -> tuple[str, int, str]:
    """shot 欄位的一個記號 → (種類, 失敗次數, 貼文連結)。種類：skip／ok／fail（最終失敗）／retry（還會再試）。"""
    from enrich.screenshot import FAIL_MARK, RETRY_MARK
    if t == SKIP:
        return "skip", 0, ""
    if t.startswith(FAIL_MARK):
        return "fail", RETRY_MAX, t[1:]
    if t.startswith(RETRY_MARK):
        m = re.match(r"\?(\d+)(.*)", t)
        if m:
            return "retry", int(m.group(1)), m.group(2)
    return "ok", 0, t


def retry_due(t: str) -> bool:
    kind, n, link = parse_token(t)
    if kind != "retry" or n >= RETRY_MAX:
        return False
    if n == 0:                      # 清理舊失敗時標的：馬上重試
        return True
    ids = records.parse_message_url(link)
    if not ids:
        return True
    import discord
    age = (datetime.now(timezone.utc) - discord.utils.snowflake_time(ids[1])).total_seconds()
    return age >= RETRY_AFTER_SEC


def shot_line(urls: list[str]) -> str:
    return SHOT_LINE + " ".join(f"[↗{i}](<{u}>)" for i, u in enumerate(urls, 1))


def success_links(shot: str | None) -> list[str]:
    """shot 欄位裡成功的截圖貼文連結。"""
    return [t for t in (shot or "").split() if parse_token(t)[0] == "ok"]


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
        self._lock = asyncio.Lock()                 # 車道處理一則鏡像、或使用者按「重試截圖」時獨占（兩邊會改同一筆紀錄）
        self._link_url: dict[str, str] = {}         # 截圖貼文連結 → 網址（鏡像箭頭用，避免重複讀 Discord）
        self.on_drained = None                    # callable()：佇列清空時通知（外部 LLM 車道等截圖做完）
        self._waiters: dict[str, list] = {}       # 鏡像連結 → 等這則截圖做完的 Future（AI 分析要拿截圖當輔助）

    # ------------------------------------------------------------ 佇列
    def needs_shot(self, e) -> bool:
        """不讀 Discord 的快速判斷：記號比網址少＝還有沒處理的網址。"""
        r = e.rec
        if r.origin == "extra":
            return False
        tokens = (r.shot or "").split()
        if any(retry_due(t) for t in tokens):          # 之前拍失敗、現在該再試了
            return True
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
                async with self._lock:
                    done_ok = await self.process(mirror)
                if not done_ok:
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
            except NetworkDown:
                self.queue.appendleft(mirror)
                self._queued.add(mirror)
                await self._wait_network(task)
            except Exception:
                log.exception("連結截圖失敗：%s", mirror)
                self._release(mirror)
            finally:
                self._holding = False

    async def sync_arrows(self, mirror_url: str, msg=None) -> bool:
        """把這則鏡像裡每個有截圖的連結，在它那行下面補上小字箭頭（只改 bot 自己發的鏡像）。回傳有沒有改。"""
        try:
            if msg is None:
                msg = await self.dc.fetch_by_url(mirror_url)
            me = self.dc.client.user
            if msg is None or me is None or msg.author.id != me.id:
                return False
            url_links: dict[str, str] = {}
            for e in self.index.by_mirror(mirror_url):
                toks = [t for t in (e.rec.shot or "").split() if parse_token(t)[0] == "ok"]
                for u, t in (await self._covered(toks)).items():
                    url_links.setdefault(u, t)
            if not url_links:
                return False
            new = add_arrows(msg.content or "", url_links)
            if new == (msg.content or "") or len(new) > 2000:
                return False
            await msg.edit(content=new)
            await asyncio.sleep(1.0)
            return True
        except Exception:
            log.warning("鏡像加截圖箭頭失敗：%s", mirror_url, exc_info=True)
            return False

    async def retry_unrenderable_failures(self) -> None:
        """一次性：以前因為「截圖工具不收 JSON／JavaScript／YAML…」失敗的通知（原因：不是可截圖的網頁（application/json…））改成馬上重試。"""
        from paths import data_dir
        from enrich.screenshot import RETRY_MARK
        flag = data_dir() / RETRY2_FLAG
        if flag.exists():
            return
        textual = ("json", "javascript", "ecmascript", "yaml", "toml", "x-sh", "sql", "x-ndjson")
        n = 0
        for e in sorted(self.index.entries.values(), key=lambda e: e.msg_id):
            toks = (e.rec.shot or "").split()
            if not any(parse_token(t)[0] in ("fail", "retry") for t in toks):
                continue
            out, changed = [], False
            for t in toks:
                kind, _, link = parse_token(t)
                if kind in ("fail", "retry"):
                    try:
                        m = await self.dc.fetch_by_url(link)
                    except Exception:
                        out.append(t)
                        continue
                    body = getattr(m, "content", "") if m is not None else ""
                    if "不是可截圖的網頁（" in body:
                        mime = body.split("不是可截圖的網頁（", 1)[1].split("，", 1)[0].split("）", 1)[0].lower()
                        if any(k in mime for k in textual):
                            t, changed = f"{RETRY_MARK}0{link}", True
                    await asyncio.sleep(0.2)
                out.append(t)
            if changed:
                await self.index.update(e.rec.key, shot=" ".join(out))
                n += 1
        flag.write_text("done")
        log.info("📸 連結截圖：%d 則紀錄因 JSON／JS／YAML 失敗的通知改成重試", n)
        self.scan_index()

    async def reset_skipped(self) -> None:
        """一次性：以前被記成「不截圖」（PDF／文字檔／圖片／JSON／YAML…）的紀錄，拿掉「不截圖」記號讓它重新判斷（現在這些都會截圖）。"""
        from paths import data_dir
        flag = data_dir() / SKIP_FLAG
        if flag.exists():
            return
        n = 0
        for e in sorted(self.index.entries.values(), key=lambda e: e.msg_id):
            toks = (e.rec.shot or "").split()
            if SKIP in toks and find_urls(e.rec.excerpt or ""):
                keep = [t for t in toks if t != SKIP]          # 拿掉所有「不截圖」記號：沒有記號的網址下次車道會重新判斷（真的不能截的又會記回去）
                await self.index.update(e.rec.key, shot=" ".join(keep) or None)
                n += 1
        flag.write_text("done")
        log.info("📸 連結截圖：%d 則紀錄的「不截圖」記號清掉，重新判斷", n)
        self.scan_index()

    async def retry_notice(self, msg) -> str:
        """使用者按了「📸 無法截圖」通知上的「重試截圖」：重拍；成功就貼出截圖、換掉紀錄裡的失敗記號、刪掉通知、補鏡像箭頭；
        失敗就把通知上的原因更新成最新的。回傳要顯示給使用者看的一行結果。"""
        import re
        from enrich.screenshot import CERT_WARN, NetworkDown, try_capture
        from enrich.shotbutton import parse_notice
        info = parse_notice(getattr(msg, "content", ""))
        if info is None:
            return "這不是截圖失敗通知，沒辦法重試"
        when, url, back = info
        async with self._lock:
            try:
                png, why = await try_capture(url)
            except NetworkDown:
                return "🔌 網路好像中斷了（連不上 google.com），等網路恢復再試"
            if not png:
                new = re.sub(r"-# 原因：.*", f"-# 原因：{why or '未知'}", msg.content, count=1)
                if new != msg.content:
                    await msg.edit(content=new)
                return f"❌ 還是失敗：{why or '未知'}"
            note = f"\n{CERT_WARN}" if why == CERT_WARN else ""
            posted = await self.dc.send_file(self.cfg.ch_screenshot,
                                             f"📸 {when}｜{url}{note}\n[↗ 跳到鏡像](<{back}>)", "preview.jpg", png)
            png = None
            old_link, new_link = msg.jump_url, posted.jump_url
            replies = set()
            for e in self.index.by_mirror(back):
                toks = (e.rec.shot or "").split()
                new = [new_link if parse_token(t)[2] == old_link else t for t in toks]
                if new != toks:
                    await self.index.update(e.rec.key, shot=" ".join(new))
                if e.rec.reply:
                    replies.add((e.rec.reply, tuple(success_links(" ".join(new)))))
            try:
                await msg.delete()
            except Exception:
                pass
            for reply_url, links in replies:
                if links:
                    await self._update_reply(reply_url, list(links))
            await self.sync_arrows(back)
        return "✅ 截圖成功，已更新"

    async def add_retry_buttons(self) -> None:
        """一次性：以前發的「無法截圖」通知也加上「重試截圖」按鈕。"""
        from paths import data_dir
        from enrich.shotbutton import RetryView
        flag = data_dir() / BUTTON_FLAG
        if flag.exists():
            return
        n = 0
        seen: set[str] = set()
        for e in sorted(self.index.entries.values(), key=lambda e: e.msg_id):
            for t in (e.rec.shot or "").split():
                kind, _, link = parse_token(t)
                if kind not in ("fail", "retry") or link in seen:
                    continue
                seen.add(link)
                try:
                    m = await self.dc.fetch_by_url(link)
                    if m is not None and not m.components:
                        await m.edit(view=RetryView())
                        n += 1
                except Exception:
                    log.warning("加「重試截圖」按鈕失敗：%s", link, exc_info=True)
                await asyncio.sleep(1.0)
        flag.write_text("done")
        log.info("📸 已為 %d 則舊的「無法截圖」通知加上「重試截圖」按鈕", n)

    async def backfill_arrows(self) -> None:
        """一次性：舊的鏡像訊息也補上截圖箭頭（慢慢做，不搶其他工作）。"""
        from paths import data_dir
        flag = data_dir() / ARROW_FLAG
        if flag.exists():
            return
        mirrors = []
        for e in sorted(self.index.entries.values(), key=lambda e: e.msg_id):
            if e.rec.mirror not in mirrors and any(parse_token(t)[0] == "ok" for t in (e.rec.shot or "").split()):
                mirrors.append(e.rec.mirror)
        n = 0
        for i, m in enumerate(mirrors, 1):
            await activity.gate("shot")
            if await self.sync_arrows(m):
                n += 1
            if i % 200 == 0:
                log.info("📸 補截圖箭頭：%d/%d", i, len(mirrors))
        flag.write_text("done")
        log.info("📸 舊鏡像補截圖箭頭完成：%d 則（共檢查 %d 則）", n, len(mirrors))

    async def _wait_network(self, task) -> None:
        """網路中斷：不發失敗通知，每 30 秒（逐次加到 5 分鐘）檢查 google.com，通了才繼續。"""
        from enrich.screenshot import network_ok
        log.warning("🔌 連結截圖：網路中斷（連不上 google.com），暫停到網路恢復")
        delay = 30.0
        while not await network_ok():
            if task is not None:
                task.set(detail="網路中斷，等待恢復")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
        if task is not None:
            task.set(detail="")
        log.info("🌐 網路已恢復，繼續連結截圖")

    async def retry_old_failures(self) -> None:
        """一次性：把舊的「無法截圖」通知（內網、不是 http、圖片太大以外）改成「馬上重試」，之後照一般流程重拍。"""
        from paths import data_dir
        from enrich.screenshot import RETRY_MARK, is_permanent
        flag = data_dir() / RETRY_FLAG
        if flag.exists():
            return
        n = 0
        for e in sorted(self.index.entries.values(), key=lambda e: e.msg_id):
            toks = (e.rec.shot or "").split()
            if not any(parse_token(t)[0] == "fail" for t in toks):
                continue
            out, changed = [], False
            for t in toks:
                kind, _, link = parse_token(t)
                if kind == "fail":
                    try:
                        m = await self.dc.fetch_by_url(link)
                    except Exception:
                        out.append(t)
                        continue
                    body = getattr(m, "content", "") if m is not None else ""
                    why = body.split("原因：", 1)[1].split("\n", 1)[0] if "原因：" in body else ""
                    if m is None or not is_permanent(why):
                        t, changed = f"{RETRY_MARK}0{link}", True
                    await asyncio.sleep(0.2)
                out.append(t)
            if changed:
                await self.index.update(e.rec.key, shot=" ".join(out))
                n += 1
        flag.write_text("done")
        log.info("📸 連結截圖：%d 則紀錄的舊失敗改成重試", n)
        self.scan_index()

    async def _covered(self, tokens: list[str]) -> dict[str, str]:
        """舊紀錄已經有的貼文：網址 → 記號（讀貼文第一行的網址），避免重拍。"""
        out: dict[str, str] = {}
        for t in tokens:
            kind, _, link = parse_token(t)
            if kind == "skip":
                continue
            if kind == "ok" and link in self._link_url:
                out.setdefault(self._link_url[link], t)
                continue
            try:
                m = await self.dc.fetch_by_url(link)
            except Exception:
                continue
            if m is None:
                continue
            for u in find_urls(m.content.split("\n", 1)[0]):
                out.setdefault(u, t)
                if kind == "ok":
                    self._link_url[link] = u
        return out

    async def process(self, mirror_url: str) -> bool:
        """處理這則鏡像所有紀錄裡還沒處理的網址。回傳 False＝暫時失敗（讀不到鏡像），稍後重試。"""
        from enrich.screenshot import FAIL_MARK, RETRY_MARK, NetworkDown, available, is_permanent, post_one
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
            text = self.text_cache.get(key) or full.get(key) or ""
            if not text:
                text = e.rec.excerpt or ""
                if text.endswith("…"):                     # 找不到全文、摘錄又被截斷：200 字之後的連結會漏掉
                    log.warning("📸 找不到 TG#%s 的全文（只有 200 字摘錄），後面的連結可能漏拍：%s", e.rec.tg_msg, mirror_url)
            urls = find_urls(text)
            old = (e.rec.shot or "").split()
            covered = await self._covered(old) if old else {}
            tokens: list[str] = []
            ok_links: list[str] = []
            down = False
            replaced: set[str] = set()
            for u in urls:
                prev_n, prev_link = 0, ""
                if u in covered:
                    kind, n, link = parse_token(covered[u])
                    if kind == "retry" and retry_due(covered[u]):
                        prev_n, prev_link = n, link             # 該再試了：重拍，成功／失敗後換掉舊通知
                        replaced.add(covered[u])
                    else:
                        tokens.append(covered[u])
                        if kind == "ok":
                            ok_links.append(covered[u])
                        continue
                if (await probe_kind(u)) in ("binary", "private"):
                    tokens.append(SKIP)
                    continue
                try:
                    link, ok, why = await post_one(self.dc, self.cfg.ch_screenshot, u, e.rec.date, mirror_url)
                except NetworkDown:
                    down = True                                  # 網路中斷：這個網址還沒處理，先存已做完的，再整則重排
                    break
                if ok:
                    tokens.append(link)
                    ok_links.append(link)
                else:
                    nn = prev_n + 1
                    final = is_permanent(why) or nn >= RETRY_MAX
                    tokens.append(f"{FAIL_MARK}{link}" if final else f"{RETRY_MARK}{nn}{link}")
                if prev_link:
                    await self.dc.delete_by_url(prev_link)
            extras = [t for t in old if t not in tokens and t not in replaced and t != SKIP]      # 對不上網址的舊貼文：留著，刪除同步才刪得到
            new = " ".join(tokens + extras) or SKIP
            if new != (e.rec.shot or ""):
                await self.index.update(key, shot=new)
            if ok_links:
                await self._update_reply(e.rec.reply, ok_links)
            log.info("📸 連結截圖：TG#%s 共 %d 個網址（成功 %d）", e.rec.tg_msg, len(urls), len(ok_links))
            if down:
                raise NetworkDown()
        await self.sync_arrows(mirror_url, msg)
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
