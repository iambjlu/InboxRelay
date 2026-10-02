# relay-core/memory.py
"""記憶（使用者用語筆記）與提示詞版本，存在 #資料庫。

- `#記憶 vN｜日期｜來源`：每一版一則訊息，內容直接放在訊息本文（不用附件，方便直接複製修改）。
  程式只認「最新一則」#記憶。使用者想蓋掉，就自己發一則以 `#記憶` 開頭的新訊息。
- 只接受 bot 本身、伺服器擁有者、或 .env MEMORY_EDITORS 裡的使用者發的 #記憶，
  避免其他成員竄改。
- 自動升級：累積一定數量的新分析結果、或收到「更正：」時，請 AI 產生新版，
  發到 #資料庫，並在通知頻道說明升級重點。
- `#提示詞 名稱 vN｜日期｜來源`：每個提示詞一則訊息，程式用「每個名稱最新的一則」。
  使用者要改，就發一則 `#提示詞 名稱` 開頭的新訊息（第二行起是內容）。舊版都留在頻道裡可回溯。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import discord

import activity
import prompts
from llm import LLM

log = logging.getLogger("memory")

def wrap_block(text: str) -> str:
    """包成程式碼區塊：Discord 不會把內容當格式渲染，也方便整段複製。"""
    return f"```\n{text.strip()}\n```"


def unwrap_block(text: str) -> str:
    """去掉外層 ```（可有語言標記）；沒有包的就原樣回傳。"""
    t = text.strip()
    if t.startswith("```") and t.endswith("```") and len(t) >= 6:
        t = t[3:-3]
        first, nl, rest = t.partition("\n")
        if nl and re.fullmatch(r"[A-Za-z0-9_+-]{1,12}", first.strip()):
            t = rest  # ```markdown 這類語言標記
        return t.strip("\n").strip()
    return t


MEM_TAG = "#記憶"
PROMPT_TAG = "#提示詞"
MAX_MEMORY_CHARS = 1800   # 要能放進一則 Discord 訊息
UPGRADE_EVERY = 30          # 累積幾筆新分析就自動升級
_VER_RE = re.compile(r"v(\d+)")


@dataclass
class Observation:
    text: str
    kw: list[str]
    gist: str


@dataclass
class MemoryStore:
    llm: LLM
    db_channel: discord.abc.Messageable
    notify_channel: discord.abc.Messageable | None
    bot_id: int
    editors: set[int]
    tz: str = "Asia/Taipei"
    version: int = 0
    content: str = ""
    observations: list[Observation] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ------------------------------------------------------------ 讀取
    def _trusted(self, m: discord.Message) -> bool:
        return m.author.id == self.bot_id or m.author.id in self.editors

    @staticmethod
    def _body(m: discord.Message) -> str:
        return unwrap_block(m.content.partition("\n")[2])

    async def load(self, scan: int = 500) -> None:
        async for m in self.db_channel.history(limit=scan):  # type: ignore[attr-defined]
            if not m.content.startswith(MEM_TAG) or not self._trusted(m):
                continue
            ver = _VER_RE.search(m.content.split("\n", 1)[0])
            self.version = int(ver.group(1)) if ver else await self._max_version(scan) + 1
            self.content = self._body(m)[:MAX_MEMORY_CHARS]
            break
        self.llm.memory, self.llm.memory_version = self.content, f"v{self.version}"
        log.info("記憶：%s（%d 字）", f"v{self.version}" if self.content else "（尚無）", len(self.content))

    async def _max_version(self, scan: int) -> int:
        best = 0
        async for m in self.db_channel.history(limit=scan):  # type: ignore[attr-defined]
            if m.content.startswith(MEM_TAG) and self._trusted(m):
                v = _VER_RE.search(m.content.split("\n", 1)[0])
                if v:
                    best = max(best, int(v.group(1)))
        return best

    def on_user_override(self, m: discord.Message) -> bool:
        """使用者在 #資料庫 手動發了新的 #記憶 → 稍後重新載入。"""
        return m.channel.id == getattr(self.db_channel, "id", None) and m.content.startswith(MEM_TAG) \
            and self._trusted(m) and m.author.id != self.bot_id

    # ------------------------------------------------------------ 升級
    def observe(self, text: str, kw: list[str], gist: str) -> bool:
        """記下一筆分析結果；回傳是否該自動升級了。"""
        if text.strip():
            self.observations.append(Observation(text.strip()[:300], kw, gist))
            self.observations = self.observations[-200:]
        return len(self.observations) >= UPGRADE_EVERY

    def add_correction(self, text: str) -> None:
        self.corrections.append(text.strip()[:500])

    def _now(self) -> str:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(self.tz)).strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def parse_upgrade(out: str) -> tuple[str, list[str]]:
        mem, _, changes = out.partition("=== 升級重點 ===")
        mem = mem.replace("=== 記憶 ===", "").strip()
        items = [ln.strip().lstrip("-•").strip() for ln in changes.strip().split("\n") if ln.strip()]
        items = [x for x in items if x and x != "無"]
        return mem[:MAX_MEMORY_CHARS], items

    def _upgrade_data(self) -> str:
        obs = "\n".join(f"- {o.text}｜關鍵字：{'、'.join(o.kw)}｜重點：{o.gist}" for o in self.observations[-80:])
        cor = "\n".join(f"- {c}" for c in self.corrections) or "（無）"
        return (f"【目前記憶 v{self.version}】\n{self.content or '（空）'}\n\n"
                f"【使用者更正】\n{cor}\n\n【最近的訊息與分析】\n{obs or '（無）'}")

    async def upgrade(self, reason: str) -> bool:
        """請 AI 產生新版記憶；有變動才發布。回傳是否升級。"""
        async with self._lock:
            if not self.observations and not self.corrections:
                return False
            if not self.corrections:
                await activity.gate()          # 自動升級算背景工作；使用者更正不等
            task = activity.start("記憶升級", total=1, background=not self.corrections)
            activity.bind(task)
            try:
                out = await self.llm.chat(prompts.get("MEMORY_UPGRADE"), self._upgrade_data(),
                                          max_tokens=2500, use_memory=False)
            finally:
                activity.finish(task)
            mem, changes = self.parse_upgrade(out)
            if not mem or (not changes and not self.corrections):
                log.info("記憶檢查：沒有需要升級的內容")
                self.observations.clear()
                return False
            await self.publish(mem, changes or ["套用使用者更正"], source=reason)
            self.observations.clear()
            self.corrections.clear()
            return True

    async def publish(self, mem: str, changes: list[str], source: str) -> None:
        ver = self.version + 1
        head = f"{MEM_TAG} v{ver}｜{self._now()}｜{source}"
        mem = mem[: 2000 - len(head) - 9].strip()
        await self.db_channel.send(f"{head}\n{wrap_block(mem)}")
        self.version, self.content = ver, mem
        self.llm.memory, self.llm.memory_version = mem, f"v{ver}"
        log.info("記憶已升級為 v%d", ver)
        if self.notify_channel is not None:
            pts = "\n".join(f"• {c}" for c in changes[:10])
            await self.notify_channel.send(
                f"🧠 **記憶已升級 v{ver}**（{source}）\n{pts}\n"
                f"-# 不喜歡可以在 #資料庫 發一則以「#記憶」開頭的新訊息蓋掉；舊版都還在頻道裡")


PROMPT_GROUPS = [
    ("基本與分析", ["GUARD", "MEMORY_WRAP", "LINK", "IMAGE", "DOC", "DOC_PART", "DOC_MERGE", "KEYWORDS"]),
    ("總結與互動", ["RECAP_DAILY", "RECAP_MONTHLY", "FEEDBACK_ROUTE", "MEMORY_UPGRADE"]),
    ("搜尋", ["SEARCH_PARSE", "SEARCH_ANSWER", "SEARCH_FOLLOWUP", "GENERAL_ANSWER"]),
]
CUSTOM_MARK = "（自訂）"
MSG_LIMIT = 1950

# 項目：「• NAME（自訂）」或「1. NAME v3」「- NAME」等，下一行起是 ``` 區塊
_ITEM_RE = re.compile(
    r"^[ \t]*(?:[•\-*]|\d+[.、)])?[ \t]*([A-Z][A-Z_]+)(?:[ \t]+v\d+)?[ \t]*(（自訂）)?[ \t]*\n```[^\n]*\n(.*?)\n?```",
    re.M | re.S)


def parse_prompt_message(content: str) -> list[tuple[str, str, bool]]:
    """解析一則 #提示詞 訊息 → [(名稱, 內容, 是否標記自訂)]。

    支援新格式（一則多個「• 名稱」＋程式碼區塊）與舊格式（「#提示詞 名稱 v3｜…」＋單一區塊）。"""
    head, _, body = content.partition("\n")
    items = [(m.group(1), m.group(3).strip(), bool(m.group(2))) for m in _ITEM_RE.finditer(body)]
    items = [(n, t, c) for n, t, c in items if n in prompts.DEFAULTS]
    if items:
        return items
    tokens = head[len(PROMPT_TAG):].split("｜")[0].split()
    if tokens and tokens[0].upper() in prompts.DEFAULTS:      # 舊格式：一則一個
        return [(tokens[0].upper(), unwrap_block(body), False)]
    return []


def build_prompt_messages(version: int, now: str, contents: dict[str, str], custom: set[str]) -> list[str]:
    """依分組產生訊息；超過長度自動拆成「（續）」。"""
    out: list[str] = []
    for group, names in PROMPT_GROUPS:
        src = "預設" if not (custom & set(names)) else "含自訂：" + "、".join(n for n in names if n in custom)
        blocks = [f"• {n}{CUSTOM_MARK if n in custom else ''}\n```\n{contents[n].strip()}\n```" for n in names]
        part, cur = 1, []
        for b in blocks:
            head = f"{PROMPT_TAG} v{version}｜{group}{'（續）' if part > 1 else ''}｜{now}｜{src}"
            if cur and len(head) + 2 + len("\n\n".join(cur + [b])) > MSG_LIMIT:
                out.append(head + "\n\n" + "\n\n".join(cur))
                part, cur = part + 1, []
            cur.append(b)
        if cur:
            head = f"{PROMPT_TAG} v{version}｜{group}{'（續）' if part > 1 else ''}｜{now}｜{src}"
            out.append(head + "\n\n" + "\n\n".join(cur))
    return out


class PromptStore:
    """#資料庫 裡的提示詞。全部提示詞分組放在少數幾則訊息，統一版號；舊版會刪掉，只留最新一批。

    修改方式：在 #資料庫 發一則以「#提示詞」開頭的訊息，裡面寫「• 名稱」＋程式碼區塊（可以只寫要改的那幾個，
    也可以整則複製來改）。程式會套用、刪掉你那則與舊的，再以新版號重發一整批；改過的標「（自訂）」。
    要改回預設：把該項內容改回預設文字即可（和預設相同就不算自訂）。
    """

    def __init__(self, db_channel: discord.abc.Messageable, notify_channel, bot_id: int,
                 editors: set[int], tz: str = "Asia/Taipei") -> None:
        self.db_channel = db_channel
        self.notify_channel = notify_channel
        self.bot_id = bot_id
        self.editors = editors
        self.tz = tz
        self.version = 0

    def _trusted(self, m: discord.Message) -> bool:
        return m.author.id == self.bot_id or m.author.id in self.editors

    def is_user_edit(self, m: discord.Message) -> bool:
        return m.channel.id == getattr(self.db_channel, "id", None) and m.content.startswith(PROMPT_TAG) \
            and self._trusted(m) and m.author.id != self.bot_id

    def _now(self) -> str:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(self.tz)).strftime("%Y-%m-%d %H:%M")

    async def load(self, publish_defaults: bool = True, scan: int = 2000) -> list[str]:
        """載入提示詞；publish_defaults=True 時會整理頻道（刪舊、發新的一批）。回傳警告。"""
        msgs: list[discord.Message] = []          # 所有 #提示詞 相關訊息（新→舊）
        async for m in self.db_channel.history(limit=scan):  # type: ignore[attr-defined]
            if m.content.startswith(PROMPT_TAG) and self._trusted(m):
                msgs.append(m)

        global_max = 0
        latest: dict[str, tuple[str, bool]] = {}   # 名稱 → (內容, 是否自訂)
        edited_names: list[str] = []
        for m in msgs:
            v = _VER_RE.search(m.content.split("\n", 1)[0])
            if v:
                global_max = max(global_max, int(v.group(1)))
            from_bot = m.author.id == self.bot_id
            for name, text, marked in parse_prompt_message(m.content):
                if name in latest:
                    continue
                is_custom = (not from_bot or marked) and text.strip() != prompts.DEFAULTS[name].strip()
                latest[name] = (text, is_custom)
                if not from_bot:
                    edited_names.append(name)

        prompts.reset()
        warnings: list[str] = []
        contents: dict[str, str] = {}
        custom: set[str] = set()
        for name, default in prompts.DEFAULTS.items():
            text, is_custom = latest.get(name, (default, False))
            if is_custom:
                err = prompts.validate(name, text)
                if err:
                    warnings.append(f"提示詞 {name}：{err}，維持預設值")
                    text, is_custom = default, False
            contents[name] = text if is_custom else default
            if is_custom:
                custom.add(name)
                prompts.set_current(name, text)

        if publish_defaults:
            expected = build_prompt_messages(global_max, "", contents, custom)
            current = [m for m in msgs if m.author.id == self.bot_id
                       and m.content.startswith(f"{PROMPT_TAG} v{global_max}｜")]
            strip_time = (lambda t: re.sub(r"｜\d{4}-\d{2}-\d{2} \d{2}:\d{2}｜", "｜｜", t, count=1))
            same = (len(current) == len(msgs) and global_max > 0
                    and sorted(strip_time(m.content) for m in current) == sorted(expected))
            if not same:
                ver = global_max + 1
                for m in msgs:                         # 刪掉所有舊的提示詞訊息（含使用者剛發的修改）
                    try:
                        await m.delete()
                    except discord.HTTPException:
                        pass
                for text in build_prompt_messages(ver, self._now(), contents, custom):
                    await self.db_channel.send(text)
                global_max = ver
                log.info("提示詞整理完成：發布 v%d（刪除舊訊息 %d 則）", ver, len(msgs))
                if edited_names and self.notify_channel is not None:
                    applied = [n for n in edited_names if n in custom]
                    if applied:
                        await self.notify_channel.send(f"📝 已套用你修改的提示詞：{'、'.join(applied)}（提示詞更新為 v{ver}）")
        self.version = global_max
        log.info("提示詞 v%d 已載入；自訂中的：%s", global_max, "、".join(sorted(custom)) or "（無，全用預設）")
        for w in warnings:
            log.warning(w)
            if self.notify_channel is not None:
                await self.notify_channel.send(f"⚠️ {w}")
        return warnings
