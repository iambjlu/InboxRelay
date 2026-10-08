# relay-core/recap/runner.py
"""每日／每月總結＋斷點補跑（第 7.7、7.8 節）。

- 每天 RECAP_TIME（預設 03:30）開始，一次把「昨天＋所有積欠的」做完，避免白天占用本地模型那台電腦。
  程式在其他時間啟動不會馬上補，會等到下一個 03:30；要立刻做：終端機按 n（立即總結）。
- 想讓出模型：終端機按 p 暫停、r 繼續（在每篇之間暫停）。
- 使用者在通知頻道要求修改總結 → 立即處理。
- 補跑：#關鍵字庫「有資料的日期」減去 #總結 裡已有 `DAILY|日期` 的日期 → 由舊到新逐日補。
  （這同時涵蓋規格的快速路徑與校正路徑，包含匯入補進的早期資料。）
- 分析未完成就等：該天還有 enriched: no、而且分析佇列還在跑 → 先跳過。
  只有最近兩天會在等滿 RECAP_WAIT_HOURS 後先總結，並標「部分內容尚未分析」，分析完成後自動更新。
- 24 小時內有訊息被編輯／刪除（RECAP_DIRTY）→ 重新總結那天（編輯原訊息），並更新該月月總結。
- 月總結：某月所有有資料的日期都有每日總結 → 產生 `MONTHLY|YYYY-MM`；
  按日期順序，該月最後一篇每日總結做完就緊接著做月總結，再做下個月。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord

import activity
import lanes
from ocr import useful
import formatting
import prompts
from recap import daily

log = logging.getLogger("recap")

TYPE_LABEL = {"text": "文字", "image": "圖片", "album": "相簿", "file": "檔案", "link": "連結",
              "voice": "語音", "video": "影片", "sticker": "貼圖", "invoice": "消費紀錄"}
_MARK_RE = re.compile(r"^(?:-# )?(DAILY|MONTHLY|YEARLY)\|(\S+?)(?: (\d+)/(\d+))?$")
_LINK_RE = re.compile(r"\[↗\d+\]\(<[^>]+>\)")
CHUNK_ROOM = 1950


def parse_marker(content: str) -> tuple[str, str, int] | None:
    m = _MARK_RE.match(content.split("\n", 1)[0].strip())
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3) or 1)


def make_chunks(kind: str, key: str, title: str, body: str, footer: str) -> list[str]:
    parts = daily.split_message(f"{title}\n{body}\n{footer}", limit=CHUNK_ROOM)
    n = len(parts)
    out = []
    for i, p in enumerate(parts, 1):
        head = f"{kind}|{key}" if i == 1 else f"-# {kind}|{key} {i}/{n}"
        out.append(f"{head}\n{p}")
    return out


def make_chunks_fit(kind: str, key: str, title: str, body: str, footer: str,
                    max_chunks: int | None = None) -> list[str]:
    """把整篇總結切成一到多則訊息（來源連結都保留）。max_chunks 有值＝不能多發訊息（這篇不在頻道最下面，
    多出來的續篇會排到別篇後面）：超過就從最後面開始把來源連結改成純文字編號，還是太長才截斷。"""
    chunks = make_chunks(kind, key, title, body, footer)
    if max_chunks is None or len(chunks) <= max_chunks:
        return chunks
    for m in reversed(list(_LINK_RE.finditer(body))):
        plain = re.sub(r"^\[(↗\d+)\].*$", r"\1", m.group(0))
        body = body[:m.start()] + plain + body[m.end():]
        chunks = make_chunks(kind, key, title, body, footer)
        if len(chunks) <= max_chunks:
            return chunks
    chunks = chunks[:max_chunks]
    chunks[-1] = chunks[-1][:POST_ROOM - 20].rsplit("\n", 1)[0] + "\n…（內容過長已截斷）"
    return chunks


PLACEHOLDER_MARK = "⏳ 內容正在總結"
POST_ROOM = 1990
DRAIN_POLL_SEC = 30          # 等分析、圖片辨識佇列做完時多久檢查一次
SETTLE_SEC = 5               # 都清空後再等這麼久確認一次（辨識完成到分析入列之間的交接空檔）


def make_post(kind: str, key: str, title: str, body: str, footer: str) -> str:
    """整篇放進「一則」訊息（才能先佔位、之後原地編輯，#總結 永遠照日期排序）。
    太長時先把後面的出處連結改成純文字編號，還是太長才截斷內文。"""
    head = f"{kind}|{key}"

    def build(b: str) -> str:
        return f"{head}\n{title}\n{b}\n{footer}".rstrip()

    text = build(body)
    if len(text) <= POST_ROOM:
        return text
    links = list(_LINK_RE.finditer(body))
    for m in reversed(links):
        plain = re.sub(r"^\[(↗\d+)\].*$", r"\1", m.group(0))
        body = body[:m.start()] + plain + body[m.end():]
        text = build(body)
        if len(text) <= POST_ROOM:
            return text
    room = POST_ROOM - len(build("")) - 12
    return build(body[:max(room, 0)].rsplit("\n", 1)[0] + "\n…（內容過長已截斷）")


def timeline_key(kind: str, key: str) -> str:
    """#總結 的排列順序：日 → 該月月總結 → … → 12 月月總結 → 年總結 → 下一年。"""
    return key if kind == "DAILY" else f"{key}-~"


def strip_citations(text: str) -> str:
    return _LINK_RE.sub("", text)


class RecapChannel:
    """#總結 頻道裡的 DAILY／MONTHLY 訊息索引。"""

    def __init__(self, channel: discord.TextChannel, bot_id: int) -> None:
        self.channel = channel
        self.bot_id = bot_id
        self.posts: dict[tuple[str, str], dict[int, discord.Message]] = {}
        self.pending: set[tuple[str, str]] = set()     # 只是佔位（⏳ 內容正在總結），還沒寫

    def has(self, kind: str, key: str) -> bool:
        """真的寫好了（不含佔位）。"""
        return (kind, key) in self.posts and (kind, key) not in self.pending

    def exists(self, kind: str, key: str) -> bool:
        """有貼文（含佔位）。"""
        return (kind, key) in self.posts

    def keys(self, kind: str) -> set[str]:
        return {k for (t, k) in self.posts if t == kind and (t, k) not in self.pending}

    def first(self, kind: str, key: str) -> discord.Message | None:
        if (kind, key) in self.pending:
            return None
        parts = self.posts.get((kind, key))
        return parts[min(parts)] if parts else None



    def body(self, kind: str, key: str) -> str:
        """這篇總結的內文（去掉標記與標題行；多則訊息接起來）。"""
        parts = self.posts.get((kind, key))
        if not parts or (kind, key) in self.pending:
            return ""
        out = []
        for i in sorted(parts):
            c = parts[i].content
            out.append(c.split("\n", 2)[-1] if i == min(parts) else c.split("\n", 1)[-1])
        return "\n".join(out)

    def is_tail(self, kind: str, key: str) -> bool:
        """這篇是不是頻道裡最後面的總結（續篇只能接在最後面才不會亂掉順序）。"""
        parts = self.posts.get((kind, key))
        if not parts:
            return True
        mine = max(m.id for m in parts.values())
        return all(m.id <= mine for ps in self.posts.values() for m in ps.values())

    async def publish_post(self, kind: str, key: str, title: str, body: str, footer: str) -> discord.Message:
        """發佈整篇總結：新的或在最後面的可以拆成多則（來源連結全保留）；中間的舊篇只能維持原本的則數。"""
        existing = len(self.posts.get((kind, key), {}))
        limit = None if (existing == 0 or self.is_tail(kind, key)) else existing
        return await self.publish(kind, key, make_chunks_fit(kind, key, title, body, footer, limit))

    async def load(self) -> None:
        self.posts.clear()
        async for m in self.channel.history(limit=None, oldest_first=True):
            if m.author.id != self.bot_id:
                continue
            mk = parse_marker(m.content)
            if mk:
                self.posts.setdefault((mk[0], mk[1]), {})[mk[2]] = m
                if mk[2] == 1 and PLACEHOLDER_MARK in m.content:
                    self.pending.add((mk[0], mk[1]))
        log.info("#總結 已有每日 %d 篇、月 %d 篇、年 %d 篇（另有 %d 則佔位）", len(self.keys("DAILY")),
                 len(self.keys("MONTHLY")), len(self.keys("YEARLY")), len(self.pending))

    async def remove(self, kind: str, key: str) -> None:
        self.pending.discard((kind, key))
        for m in self.posts.pop((kind, key), {}).values():
            try:
                await m.delete()
            except discord.NotFound:
                pass

    def forget_message(self, message_id: int) -> bool:
        """使用者在 #總結 手動刪了某篇 → 從索引移除（下次檢查會重新產生）。"""
        for k, parts in list(self.posts.items()):
            for i, m in list(parts.items()):
                if m.id == message_id:
                    del parts[i]
                    if 1 not in parts:          # 第一則（含標記）沒了就整篇視為不存在
                        self.posts.pop(k, None)
                        self.pending.discard(k)
                    return True
        return False

    async def publish(self, kind: str, key: str, chunks: list[str]) -> discord.Message:
        """已存在就依序編輯（多的刪掉、少的補發），否則新發。"""
        old = self.posts.get((kind, key), {})
        new: dict[int, discord.Message] = {}
        for i, c in enumerate(chunks, 1):
            if i in old:
                new[i] = await old[i].edit(content=c, suppress=True)
            else:
                new[i] = await self.channel.send(c, suppress_embeds=True)
        for i, m in old.items():
            if i not in new:
                try:
                    await m.delete()
                except discord.NotFound:
                    pass
        self.posts[(kind, key)] = new
        self.pending.discard((kind, key))
        return new[1]


class Recapper:
    def __init__(self, cfg, llm, index, recap_ch: RecapChannel, state_rec, dirty, enricher=None,
                 wait_hours: float = 12, notes=None) -> None:
        self.cfg = cfg
        self.llm = llm
        self.index = index
        self.rc = recap_ch
        self.state = state_rec     # state.DbRecord("STATE")
        self.dirty = dirty         # state.DirtyDays
        self.enricher = enricher
        self.wait_hours = wait_hours
        self.tz = ZoneInfo(cfg.tz)
        self._lock = asyncio.Lock()
        self.notes = notes         # state.DbRecord("RECAP_NOTES")：日期／月份 → 使用者補充要求
        self.waits: list = []              # 其他要先做完才能開始總結的佇列：callable() → True＝已做完（例如圖片辨識 OcrWorker.drained）
        self.start_at = None               # datetime.time；每天幾點開始
        self._trigger: asyncio.Event | None = None
        self._cycle = False                # 這一輪（總結→等分析→再總結）還在進行

    def set_schedule(self, start: str) -> None:
        from datetime import time as dtime
        h, m = (int(x) for x in start.split(":"))
        self.start_at = dtime(h, m)

    def seconds_until_start(self, now: datetime | None = None) -> float:
        now = now or self._now()
        if self.start_at is None:
            return 0.0
        start = now.replace(hour=self.start_at.hour, minute=self.start_at.minute, second=0, microsecond=0)
        if start <= now:
            start += timedelta(days=1)
        return (start - now).total_seconds()

    @property
    def phase(self) -> str:
        """running＝正在總結；queued＝已按「立即總結」、還沒開始；""＝閒置。"""
        if self._lock.locked() or getattr(self, "_cycle", False):
            return "running"
        lane = lanes.get("llm")
        if lane is not None and lane.queued:
            return "queued"
        return ""

    def trigger_now(self) -> bool:
        """立即總結（終端機按 n／App 按鈕）。正在總結或已經排隊時不重複觸發，回傳 False。"""
        if self.phase:
            return False
        lane = lanes.get("llm")
        return lane.run_now() if lane is not None else False

    def note_for(self, key: str) -> str:
        return (self.notes.data.get(key, "") if self.notes else "").strip()

    async def add_note(self, key: str, instruction: str) -> None:
        if not self.notes or not instruction.strip():
            return
        old = self.note_for(key)
        merged = f"{old}；{instruction.strip()}" if old else instruction.strip()
        await self.notes.merge(**{key: merged})

    def _with_note(self, prompt: str, key: str) -> str:
        note = self.note_for(key)
        if not note:
            return prompt
        return (prompt + "\n\n使用者對這篇的補充要求（務必遵守；但材料裡沒有的內容仍不可編造，"
                f"找不到就在文末註明「材料中找不到：…」）：{note}")

    # ------------------------------------------------------------ 資料
    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _entries(self, day: str) -> list:
        return sorted((e for e in self.index.entries.values() if e.rec.date[:10] == day),
                      key=lambda e: (e.rec.date, e.rec.tg_msg))

    def data_days(self) -> list[str]:
        return sorted({e.rec.date[:10] for e in self.index.entries.values() if e.rec.date})

    def _partial(self) -> set[str]:
        return {x.strip() for x in self.state.data.get("partial_days", "").split(",") if x.strip()}

    async def _set_partial(self, days: set[str]) -> None:
        await self.state.merge(partial_days=", ".join(sorted(days)))

    @staticmethod
    def item_text(rec) -> str:
        bits = []
        if rec.excerpt:
            bits.append(rec.excerpt)
        if rec.filename:
            bits.append(f"檔案：{rec.filename}")
        if rec.gist:
            bits.append(f"重點：{rec.gist}")
        if rec.kw:
            bits.append(f"關鍵字：{rec.kw}")
        o = useful(getattr(rec, "ocr", None))
        if o:
            bits.append(f"圖中文字：{o[:200]}")
        return "｜".join(bits) or "（無文字）"

    # ------------------------------------------------------------ 產生
    async def summarize_day(self, day: str) -> None:
        entries = self._entries(day)
        if not entries:
            if self.rc.has("DAILY", day):       # 那天的訊息都被刪光了 → 總結也撤掉
                await self.rc.remove("DAILY", day)
                log.info("%s 已沒有訊息，撤除每日總結", day)
            return
        items = [daily.Item(e.rec.date[11:16], TYPE_LABEL.get(e.rec.type, e.rec.type),
                            self.item_text(e.rec), e.rec.mirror) for e in entries]
        out = await self.llm.chat(self._with_note(daily.build_prompt(day), day), daily.build_material(items),
                                  max_tokens=2500)
        body = daily.link_citations(out, [it.url for it in items])
        un = sum(1 for e in entries if not e.rec.enriched)
        footer = f"-# 共 {len(entries)} 則訊息" + ("｜⚠️ 部分內容尚未分析，分析完成後會自動更新" if un else "")
        await self.rc.publish_post("DAILY", day, f"# 📰 {formatting.display_date(day)} 每日總結", body, footer)
        partial = self._partial()
        (partial.add if un else partial.discard)(day)
        await self._set_partial(partial)
        done = self.rc.keys("DAILY")
        await self.state.merge(summarized_until=max(done) if done else "")
        log.info("每日總結 %s 完成（%d 則%s）", day, len(entries), f"，{un} 則未分析" if un else "")

    async def summarize_month(self, month: str) -> None:
        days = sorted(d for d in self.rc.keys("DAILY") if d.startswith(month))
        items = []
        for d in days:
            m = self.rc.first("DAILY", d)
            if m is None:
                continue
            text = strip_citations(m.content.split("\n", 2)[-1])[:600]
            items.append(daily.Item(d[5:], "每日總結", text, m.jump_url))
        if not items:
            return
        out = await self.llm.chat(self._with_note(prompts.render("RECAP_MONTHLY", month=month), month),
                                  daily.build_material(items), max_tokens=3000)
        body = daily.link_citations(out, [it.url for it in items])
        footer = f"-# 根據 {len(items)} 天的每日總結"
        await self.rc.publish_post("MONTHLY", month, f"# 🗓️ {month} 月總結", body, footer)
        done = self.rc.keys("MONTHLY")
        await self.state.merge(monthly_until=max(done) if done else "")
        log.info("月總結 %s 完成（%d 天）", month, len(items))

    async def summarize_year(self, year: str) -> None:
        months = sorted(m for m in self.rc.keys("MONTHLY") if m.startswith(year))
        items = []
        for mo in months:
            m = self.rc.first("MONTHLY", mo)
            if m is None:
                continue
            text = strip_citations(m.content.split("\n", 2)[-1])[:1200]
            items.append(daily.Item(mo, "月總結", text, m.jump_url))
        if not items:
            return
        out = await self.llm.chat(self._with_note(prompts.render("RECAP_YEARLY", year=year), year),
                                  daily.build_material(items), max_tokens=4000)
        body = daily.link_citations(out, [it.url for it in items])
        footer = f"-# 根據 {len(items)} 個月的月總結"
        await self.rc.publish_post("YEARLY", year, f"# 🎆 {year} 年總結", body, footer)
        log.info("年總結 %s 完成（%d 個月）", year, len(items))

    # ------------------------------------------------------------ 排程
    async def tick(self) -> None:
        async with self._lock:
            try:
                await self.dirty.load()      # 匯入工具可能新增了「需要重新總結」的日期
            except Exception:
                log.warning("重讀 RECAP_DIRTY 失敗，沿用記憶體中的清單")
            await self._tick(self._now())

    def plan(self, now: datetime) -> list[tuple[str, str]]:
        """列出這次要做的每日總結：(種類, 日期)。種類：redo（被編輯／刪除）、partial（剛分析完）、daily（缺少）。"""
        today = now.strftime("%Y-%m-%d")
        recent_floor = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        busy = bool(self.enricher and self.enricher.backlog > 0)

        def unenriched(day: str) -> int:
            return sum(1 for e in self._entries(day) if not e.rec.enriched)

        work: list[tuple[str, str]] = []
        for d in sorted(self.dirty.dates):
            if d < today and self.rc.has("DAILY", d):
                work.append(("redo", d))
        for d in sorted(self._partial()):
            if not unenriched(d) and self.rc.has("DAILY", d) and ("redo", d) not in work:
                work.append(("partial", d))
        for d in self.data_days():
            if d >= today or self.rc.has("DAILY", d):
                continue
            if unenriched(d) and busy:
                end = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=self.tz) + timedelta(days=1)
                waited = (now - end).total_seconds() / 3600
                if not (d >= recent_floor and waited >= self.wait_hours):
                    continue  # 等分析完成
            work.append(("daily", d))
        return work

    async def reset_all(self) -> int:
        """清空 #總結（bot 發的全部）並重設進度；之後等分析全部做完，再依日期一次重新總結。"""
        from reset import purge_bot_messages
        async with self._lock:
            n = await purge_bot_messages(self.rc.channel, self.rc.bot_id)
            self.rc.posts.clear()
            self.rc.pending.clear()
            await self.state.merge(summarized_until="", monthly_until="", partial_days="")
            await self.dirty.clear_all()
            log.info("🧹 已清空 #總結（%d 則）；等外部 LLM 把分析全部做完後，會依日期重新總結", n)
        return n

    async def _tick(self, now: datetime) -> None:
        """依日期順序做：某個月最後一篇每日總結做完，緊接著就做那個月的月總結，再往下一個月。"""
        today = now.strftime("%Y-%m-%d")
        this_month = today[:7]
        work = self.plan(now)

        # 今天以前、或已沒有資料的「待重寫」日期，處理後清掉
        for d in sorted(self.dirty.dates):
            if (d < today and not self.rc.has("DAILY", d)) or not self._entries(d):
                await self.dirty.clear(d)

        days = self.data_days()
        touched = {d[:7] for _, d in work}
        months = sorted(m for m in ({d[:7] for d in days} - {this_month})
                        if m < this_month and (m in touched or not self.rc.has("MONTHLY", m)))
        # 年總結：今年以前、這次有動到的月份所在年份，或還沒有年總結的年份
        this_year = today[:4]
        touched_years = {m[:4] for m in months}
        years = sorted(y for y in {d[:4] for d in days}
                       if y < this_year and (y in touched_years or not self.rc.has("YEARLY", y)))
        # 時間軸：每日總結用日期排序；月總結排在該月所有日期之後（"YYYY-MM-~" 比 "YYYY-MM-31" 大）；
        # 年總結排在該年 12 月的月總結之後（"YYYY-~" 比 "YYYY-12-~" 大、比下一年小）
        steps = sorted([(d, kind) for kind, d in work] + [(f"{m}-~", "month") for m in months]
                       + [(f"{y}-~", "year") for y in years])
        # 順序檢查：新貼文只能接在最後；比現有最後一篇還早的日期（例如事後補匯入）只能排在後面，記錄警告
        latest = max((timeline_key(k, v) for (k, v) in self.rc.posts), default="")
        kind_of = {"month": "MONTHLY", "year": "YEARLY"}
        late = [k for k, kind in steps
                if k < latest and not self.rc.exists(kind_of.get(kind, "DAILY"), k.rstrip("-~"))]
        if late:
            log.warning("有 %d 篇總結的日期比 #總結 最後一篇還早（例：%s），只能排在後面", len(late), "、".join(late[:3]))

        def year_ready(y: str) -> bool:
            ms = {d[:7] for d in self.data_days() if d.startswith(y)}
            return bool(ms) and all(self.rc.has("MONTHLY", m) for m in ms)

        def month_ready(m: str) -> bool:
            ds = [d for d in self.data_days() if d.startswith(m)]
            return bool(ds) and all(self.rc.has("DAILY", d) for d in ds)

        task = activity.start("每日／月／年總結", total=len(steps)) if steps else None
        activity.bind(task)
        try:
            for key, kind in steps:
                await activity.gate()
                if kind == "year":
                    y = key[:4]
                    if task:
                        task.set(detail=f"年總結 {y}")
                    if year_ready(y):
                        await self.summarize_year(y)
                    else:
                        log.info("%s 年還有月份缺月總結，年總結下次再做", y)
                elif kind == "month":
                    m = key[:7]
                    if task:
                        task.set(detail=f"月總結 {m}")
                    if month_ready(m):
                        await self.summarize_month(m)
                    else:
                        log.info("%s 還有日期缺每日總結（等分析完成），月總結下次再做", m)
                else:
                    if task:
                        task.set(detail={"redo": "重寫", "partial": "補完", "daily": "產生"}[kind] + f" {key}")
                    await self.summarize_day(key)
                    if kind == "redo":
                        await self.dirty.clear(key)
                if task:
                    task.advance()
        finally:
            if task:
                activity.finish(task)

    def ready(self) -> bool:
        """分析佇列和其他要先做完的佇列（圖片辨識…）都清空了。"""
        return (self.enricher is None or self.enricher.drained) and all(w() for w in self.waits)

    async def wait_ready(self) -> None:
        """總結要等「圖片辨識」和「AI 分析」都做完：辨識出文字會觸發重新分析，所以要連續兩次確認都清空才算（避免剛好卡在交接空檔）。"""
        while True:
            if self.ready():
                await asyncio.sleep(SETTLE_SEC)
                if self.ready():
                    return
            else:
                await asyncio.sleep(DRAIN_POLL_SEC)

    async def cycle(self) -> None:
        """外部 LLM 車道每次開始時做：等分析佇列全部做完 → 依日期順序一次發完囤積的日／月／年總結。
        （邊分析邊發會讓 #總結 缺日子、日期跳來跳去，因為 Discord 沒辦法插回舊位置。）"""
        self._cycle = True
        try:
            await self.wait_ready()
            await self.tick()
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            from llm import is_offline_error
            if is_offline_error(ex):                 # 外部 LLM 連不上：沒寫的總結留到下次（下次會自動補）
                lane = lanes.get("llm")
                if lane is not None:
                    lane.go_offline(type(ex).__name__)
            else:
                log.exception("總結失敗")
        finally:
            self._cycle = False
