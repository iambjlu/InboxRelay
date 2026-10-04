# relay-core/chat.py
"""AI 對話頻道（第 7.12 節）：像用 ChatGPT 一樣跟外部 LLM 對話。

- 在對話頻道直接發訊息（文字、圖片、文件附件都可以）→ bot 以那則訊息開一個「對話串」並在裡面回覆；
  之後在對話串裡繼續聊。每個對話串是一段獨立的對話。
- 每次回覆都把「提示詞＋記憶＋這個對話串的完整歷史」交給 LLM（OpenAI 多輪格式：user／assistant）。
  歷史太長（超過 MAX_HISTORY_CHARS）才會省略中間較舊的部分（保留第一則與最近的）。
- 圖片：最近 MAX_IMAGES 張以圖片交給模型（模型支援看圖時）；更早的只標檔名。
- 文件（txt／程式碼／PDF／Office…）：抽出文字放進對話（每份最多 DOC_CHARS 字）。
- 最新一則裡的連結：抓網頁正文附上（不另外叫 AI 摘要）。
- 訊息開頭寫「/思考」→ 這次讓模型先思考（較慢、較準）。
- 提示詞與記憶放在提示詞頻道：只讀「最後一則」#提示詞 與「最後一則」#記憶（各自最多一則 Discord 訊息的長度）。
  你可以直接在那裡發新的 #提示詞／#記憶 蓋掉。
- 記憶自動升級（不算則數）：模型回答時順便判斷「這輪有沒有值得長期記住的事」（使用者要求記住、介紹背景、
  長期偏好、糾正回答方式、進行中的專案、糾正錯誤認知），有的話在回覆最後加一行「〔記憶〕…」（bot 會拿掉、不顯示）。
  使用者明確說「記住／以後…」→ 馬上升級；其他的等對話安靜 IDLE_UPGRADE_SEC 秒後一次整理。沒有值得記的就不升級。
- 「➕ 新對話」按鈕：頻道裡有一則釘選的按鈕訊息，一鍵開新對話串（名稱先用「日期_亂碼」）；第一輪對話後由 AI 依內容
  把名稱改成「日期_標題」。從主頻道直接發訊息開的對話串，也會在第一輪後用 AI 重新命名（自己改過名字的不會被蓋掉）。
- 按「新對話」開出來的對話串（以及分支出來的）最上面有一則控制訊息，三顆按鈕：➕ 新增聊天、🔀 從這裡分支、🗑️ 刪除此對話。
  分支＝開新對話串並把目前的對話搬過去（使用者看到完整內容；曾經壓縮過的話，LLM 仍只讀最後一次摘要之後的部分）；
  刪除會先發一則確認／取消訊息：確認＝刪掉整個對話串，取消＝只刪掉那則確認訊息。
- 互動功能：不受「外部 LLM」排程限制，而且優先於背景工作。
- 每則回覆最下面用小字顯示用了多少 context（端點回報的實際 token 數）。
- 歷史依 context window（設定 LLM_CONTEXT）分配，超過才省略中間較舊的；用到 CHAT_COMPACT_PCT% 時自動壓縮：
  請模型把目前為止的對話濃縮成「🗜️ 對話摘要」貼在對話串裡，之後從摘要接著聊（摘要之前的訊息不再送出）。
  對話串裡打「/compact」可以手動壓縮；壓縮時對話串裡會顯示文字進度條，完成後自動刪掉。
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import random
import re
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import activity
import prompts
from memory import MEM_TAG, PROMPT_TAG, MemoryStore, unwrap_block, wrap_block
from recap import daily

log = logging.getLogger("chat")

MAX_HISTORY_CHARS = 60_000     # 沒指定時的文字歷史上限（實際依 context window 計算）
MAX_IMAGES = 4                 # 最近幾張圖片以圖片交給模型
DOC_CHARS = 20_000             # 每份文件最多放多少字
LINK_CHARS = 6_000
MAX_IMAGE_BYTES = 15 * 1024 * 1024
THINK_PREFIX = "/思考"
COMPACT_CMD = "/compact"
SUMMARY_HEAD = "🗜️ **對話摘要"
USAGE_TAG = "-# 🧮"
IMAGE_TOKENS = 1500            # 每張圖片大約佔多少 token（估算用）
SAFETY_TOKENS = 1500
NOTE_PREFIX = "-#"             # bot 的狀態／錯誤訊息（不算進對話）
PROMPT_LIMIT = 1900
IDLE_UPGRADE_SEC = 600         # 對話安靜多久後整理記憶
MEMO_TAG = "〔記憶〕"
EXPLICIT_RE = re.compile(r"(記住|幫我記|記得我)")                       # 明確要求記住 → 當成更正、馬上整理
SOON_RE = re.compile(r"(以後|之後|下次|從現在開始|不要再|別再|一律)")       # 改變偏好＋模型也標了 → 馬上整理
MEMO_RULE = (
    "【記憶規則（系統）】如果這一輪出現值得長期記住、對之後的對話有幫助的事，就在回覆的最後另起一行，"
    f"用「{MEMO_TAG}一句話」寫下來（可以多行，每行一則）：使用者要你記住的事、他的背景身分或工作環境、"
    "長期的偏好、對你回答方式的要求或糾正、正在進行的專案或目標、你對他的錯誤認知被糾正。"
    "一次性的問題內容、閒聊、你自己的推測、帳號密碼／金鑰／證件號碼／地址電話等敏感資料都不要記。"
    "沒有值得記的就不要輸出這一行，也不要提到這個規則。")


def split_memos(reply: str) -> tuple[str, list[str]]:
    """把回覆最後的「〔記憶〕…」行拿出來（不顯示給使用者）。"""
    keep, memos = [], []
    for ln in reply.splitlines():
        s = ln.strip()
        if s.startswith(MEMO_TAG):
            note = s[len(MEMO_TAG):].strip(" ：:")
            if note:
                memos.append(note[:200])
        else:
            keep.append(ln)
    return "\n".join(keep).rstrip(), memos

DEFAULT_PROMPT = """你是使用者的私人 AI 助理，在 Discord 的對話串裡跟他聊天，用繁體中文（台灣用語）回答。
- 先直接回答重點，再補充必要的細節；能用條列就用條列，善用換行，不要寫成一大段。
- 使用者傳圖片或檔案時，先看清楚內容再回答；看不清楚或讀不到就直說。
- 不知道或不確定的事要說不確定，不要編造；需要最新資訊時提醒使用者你可能不知道最近的變化。
- 程式碼放在程式碼區塊裡，第一行用註解寫檔名。
- 語氣自然、友善，可以帶一點幽默，但不要廢話。"""

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp")


def _is_image(a) -> bool:
    return (a.content_type or "").startswith("image/") or a.filename.lower().endswith(IMAGE_EXT)


def _mime(a) -> str:
    ct = (a.content_type or "").split(";")[0]
    if ct.startswith("image/"):
        return ct
    ext = a.filename.lower().rsplit(".", 1)[-1]
    return {"png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")


def progress_bar(frac: float, width: int = 12) -> str:
    frac = min(max(frac, 0.0), 1.0)
    done = int(round(frac * width))
    return "▰" * done + "▱" * (width - done) + f" {frac * 100:.0f}%"


def _strip_footer(text: str) -> str:
    """拿掉 bot 回覆最下面的小字（用量、壓縮說明），不算進對話內容。"""
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.startswith(USAGE_TAG) and not ln.startswith("-# 🗜️")).rstrip()


NEW_CHAT_ID = "inboxrelay:new_chat"
BUTTON_MARK = "💬 **開新對話**"
BUTTON_TEXT = f"{BUTTON_MARK}\n-# 按下面的按鈕開一個新的對話串（名稱第一輪聊完會自動換成「日期_標題」），或直接在這個頻道發訊息。"
_PLACEHOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}_[a-z0-9]{4}$")
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
TITLE_PROMPT = ("根據下面這段對話的開頭，取一個簡短的繁體中文標題（8～20 個字，不要標點符號結尾、不要引號、"
                "不要加日期），只輸出標題本身。")


def placeholder_name(day: str) -> str:
    return f"{day}_{''.join(random.choice(_ALPHABET) for _ in range(4))}"


def is_placeholder(name: str) -> bool:
    return bool(_PLACEHOLDER.match(name or ""))


def clean_title(raw: str) -> str:
    t = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    t = re.sub(r"^(標題|Title)\s*[:：]\s*", "", t.strip(), flags=re.I).strip(" \"'「」『』“”`*#")
    t = re.sub(r"[\\/]", "", t).rstrip("。.!！?？,，;；:：")
    return t[:30]


USER_HEAD = "👤 **"            # 分支搬移時，使用者的發言由 bot 重貼；開頭標記讓 LLM 仍把它當成使用者說的話


def is_copied_user(content: str) -> bool:
    return (content or "").startswith(USER_HEAD)


def copied_user_text(content: str) -> str:
    return content.split("\n", 1)[1] if "\n" in content else ""


class ThreadControlView(discord.ui.View):
    """對話串裡的控制訊息：新增聊天／從這裡分支／刪除此對話（持久化，bot 重啟後仍有效）。"""
    def __init__(self, chat: "ChatChannel") -> None:
        super().__init__(timeout=None)
        self.chat = chat

    @discord.ui.button(label="新增聊天", emoji="➕", style=discord.ButtonStyle.primary, custom_id="inboxrelay:t_new")
    async def t_new(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.chat.open_thread(interaction)

    @discord.ui.button(label="從這裡分支", emoji="🔀", style=discord.ButtonStyle.secondary,
                       custom_id="inboxrelay:t_branch")
    async def t_branch(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.chat.branch_thread(interaction)

    @discord.ui.button(label="刪除此對話", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="inboxrelay:t_delete")
    async def t_delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.chat.ask_delete(interaction)


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, chat: "ChatChannel", thread: discord.Thread, requester: int) -> None:
        super().__init__(timeout=120)
        self.chat, self.thread, self.requester = chat, thread, requester
        self.message: discord.Message | None = None

    async def _mine(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester:
            await interaction.response.send_message("只有按下「刪除此對話」的人可以確認或取消。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="確認刪除", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._mine(interaction):
            self.stop()
            await self.chat.delete_thread(interaction, self.thread)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._mine(interaction):
            self.stop()
            await interaction.response.defer()
            try:
                await interaction.message.delete()          # type: ignore[union-attr]
            except discord.HTTPException:
                pass

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass


class NewChatView(discord.ui.View):
    def __init__(self, chat: "ChatChannel") -> None:
        super().__init__(timeout=None)
        self.chat = chat

    @discord.ui.button(label="新對話", emoji="➕", style=discord.ButtonStyle.primary, custom_id=NEW_CHAT_ID)
    async def new_chat(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.chat.open_thread(interaction)


def thread_title(text: str, fallback: str = "新對話") -> str:
    t = " ".join(text.replace(THINK_PREFIX, "").split())
    return (t[:40] + "…") if len(t) > 40 else (t or fallback)


def merge_roles(turns: list[dict]) -> list[dict]:
    """相鄰同一角色的訊息合併（使用者連發幾則、bot 回覆被拆成幾則）。"""
    out: list[dict] = []
    for t in turns:
        if out and out[-1]["role"] == t["role"]:
            prev = out[-1]["content"]
            cur = t["content"]
            if isinstance(prev, str) and isinstance(cur, str):
                out[-1]["content"] = prev + "\n\n" + cur
            else:
                pl = prev if isinstance(prev, list) else [{"type": "text", "text": prev}]
                cl = cur if isinstance(cur, list) else [{"type": "text", "text": cur}]
                out[-1]["content"] = pl + cl
        else:
            out.append(dict(t))
    return out


def _text_len(t: dict) -> int:
    c = t["content"]
    if isinstance(c, str):
        return len(c)
    return sum(len(p.get("text", "")) for p in c if p.get("type") == "text")


def trim_history(turns: list[dict], limit: int = MAX_HISTORY_CHARS) -> list[dict]:
    """太長時保留第一則與最近的，中間較舊的省略。"""
    if sum(_text_len(t) for t in turns) <= limit or len(turns) <= 2:
        return turns
    first, rest = turns[0], turns[1:]
    kept: list[dict] = []
    total = _text_len(first)
    for t in reversed(rest):
        n = _text_len(t)
        if total + n > limit and kept:
            break
        kept.append(t)
        total += n
    kept.reverse()
    dropped = len(rest) - len(kept)
    note = {"role": "user", "content": f"（中間有 {dropped} 則較舊的對話因為太長而省略）"}
    return merge_roles([first, note] + kept)


class ChatChannel:
    def __init__(self, cfg, dc, llm, editors: set[int], notify_channel=None) -> None:
        self.cfg = cfg
        self.dc = dc
        self.llm = llm
        self.editors = editors
        self.notify_channel = notify_channel
        self.channel_id = cfg.ch_chat
        self.prompt_channel_id = cfg.ch_chat_prompt
        self.prompt = DEFAULT_PROMPT
        self.prompt_version = 0
        self.memory: MemoryStore | None = None
        self._docs: OrderedDict[int, str] = OrderedDict()     # 附件 ID → 抽出的文字（最近 50 份）
        self._starters: dict[int, discord.Message] = {}       # 對話串 ID → 開頭那則（在主頻道）
        self._locks: dict[int, asyncio.Lock] = {}
        self._idle_task: asyncio.Task | None = None
        self._tpc = 1.0                                     # 每個字大約幾個 token（用端點回報的實際用量校正）
        self._sleeping = False
        self._auto: set[int] = set()                         # 這次執行期間自動命名、還沒用 AI 改名的對話串
        self._retitling: set[int] = set()

    @property
    def bot_id(self) -> int:
        return self.dc.client.user.id  # type: ignore[union-attr]

    def _trusted(self, m: discord.Message) -> bool:
        return m.author.id == self.bot_id or m.author.id in self.editors

    # ------------------------------------------------------------ 提示詞／記憶
    async def setup(self) -> None:
        pch = await self.dc.channel(self.prompt_channel_id)
        self.memory = MemoryStore(
            llm=self.llm, db_channel=pch, notify_channel=self.notify_channel, bot_id=self.bot_id,
            editors=self.editors, tz=self.cfg.tz, apply_to_llm=False, upgrade_prompt="CHAT_MEMORY_UPGRADE",
            compress_hint="保留【關於使用者】【偏好的回答方式】【進行中的事】【待確認】四段與使用者的更正；"
                          "一次性的問題細節刪掉。",
            where="AI 對話提示詞頻道", label="AI 對話記憶", obs_label="這段時間對話中值得記住的事")
        await self.memory.load()
        asyncio.create_task(self.memory.refresh_once(
            "chat-memory-rules-v2", "待確認最多 3 條，只放會影響之後怎麼回答、問一次就能確認的事；"
            "不要放專案或事件的後續進度、一次性的事、單純的數字紀錄。"))
        await self.load_prompt(publish_default=True)
        self.dc.add_message_handler(self.on_message)
        await self._ensure_button()
        log.info("AI 對話已啟用：頻道 %s（提示詞 v%d，記憶 %s）", self.channel_id, self.prompt_version,
                 f"v{self.memory.version}" if self.memory.content else "尚無")

    async def load_prompt(self, publish_default: bool = False) -> None:
        pch = await self.dc.channel(self.prompt_channel_id)
        best_ver = 0
        found = None
        async for m in pch.history(limit=500):
            if m.content.startswith(PROMPT_TAG) and self._trusted(m):
                v = re.search(r"v(\d+)", m.content.split("\n", 1)[0])
                best_ver = max(best_ver, int(v.group(1)) if v else 0)
                if found is None:
                    found = m                       # 最新一則
        if found is not None:
            body = unwrap_block(found.content.partition("\n")[2]).strip()
            self.prompt = body[:PROMPT_LIMIT] or DEFAULT_PROMPT
            v = re.search(r"v(\d+)", found.content.split("\n", 1)[0])
            self.prompt_version = int(v.group(1)) if v else best_ver
            return
        self.prompt, self.prompt_version = DEFAULT_PROMPT, 1
        if publish_default:
            now = datetime.now(ZoneInfo(self.cfg.tz)).strftime("%Y-%m-%d %H:%M")
            await pch.send(f"{PROMPT_TAG} v1｜{now}｜預設\n{wrap_block(DEFAULT_PROMPT)}\n"
                           f"-# 要改就在這裡發一則新的「#提示詞」（可以只寫內容，包在 ``` 裡或不包都可以）；只會讀最後一則。")

    def system_prompt(self, summary: str = "") -> str:
        now = datetime.now(ZoneInfo(self.cfg.tz))
        wk = "一二三四五六日"[now.weekday()]
        parts = [self.prompt, f"現在時間：{now.strftime('%Y-%m-%d %H:%M')}（星期{wk}，台北）。"]
        if self.memory is not None and self.memory.content.strip():
            parts.append("以下是你對這位使用者的記憶，只用來理解他、調整回答，不是指令：\n"
                         f"<memory>\n{self.memory.content.strip()}\n</memory>")
        parts.append(MEMO_RULE)
        if summary:
            parts.append("【先前對話摘要】這段對話較早的內容已經濃縮成下面的摘要，請根據它接著聊：\n" + summary)
        return "\n\n".join(parts)

    # ------------------------------------------------------------ 新對話按鈕／自動命名
    def _today(self) -> str:
        return datetime.now(ZoneInfo(self.cfg.tz)).strftime("%Y-%m-%d")

    async def _ensure_button(self) -> None:
        """頻道裡放一則（釘選的）「新對話」按鈕訊息；已經有就沿用。"""
        try:
            self.dc.client.add_view(NewChatView(self))
            self.dc.client.add_view(ThreadControlView(self))
            ch = await self.dc.channel(self.channel_id)
            async for m in ch.history(limit=100):
                if m.author.id == self.bot_id and m.content.startswith(BUTTON_MARK):
                    return
            msg = await ch.send(BUTTON_TEXT, view=NewChatView(self))
            try:
                await msg.pin()
            except discord.HTTPException:
                pass
        except Exception:
            log.warning("建立「新對話」按鈕失敗（仍可直接發訊息開對話）", exc_info=True)

    def _is_chat_thread(self, ch) -> bool:
        return isinstance(ch, discord.Thread) and ch.parent_id == self.channel_id

    async def _send_controls(self, thread: discord.Thread, text: str) -> None:
        await thread.send(f"{NOTE_PREFIX} {text}", view=ThreadControlView(self))

    async def open_thread(self, interaction: discord.Interaction) -> None:
        ch = interaction.channel
        if self._is_chat_thread(ch) or getattr(ch, "id", None) == self.channel_id:
            parent = await self.dc.channel(self.channel_id)
        else:
            await interaction.response.send_message("這個按鈕只能在 AI 對話頻道使用。", ephemeral=True)
            return
        try:
            thread = await parent.create_thread(name=placeholder_name(self._today()),  # type: ignore[attr-defined]
                                                type=discord.ChannelType.public_thread, auto_archive_duration=10080)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"⚠️ 開對話串失敗：{str(e)[:150]}", ephemeral=True)
            return
        self._auto.add(thread.id)
        await interaction.response.send_message(f"✅ 已開新對話：{thread.mention}", ephemeral=True)
        await self._send_controls(thread, "💬 直接在這裡發訊息就能開始聊；第一輪聊完會自動把對話串名稱改成「日期_標題」。")

    # ------------------------------------------------------------ 分支／刪除
    async def _collect(self, thread: discord.Thread) -> list[discord.Message]:
        """對話串裡算進對話的訊息（開頭那則＋整個對話串，不含 bot 的狀態訊息），依時間順序。"""
        msgs: list[discord.Message] = []
        starter = await self._starter(thread)
        if starter is not None:
            msgs.append(starter)
        async for h in thread.history(limit=None, oldest_first=True):
            if h.type not in (discord.MessageType.default, discord.MessageType.reply):
                continue
            if starter is not None and h.id == starter.id:
                continue
            if h.author.bot and (h.author.id != self.bot_id or h.content.startswith(NOTE_PREFIX)):
                continue
            msgs.append(h)
        return msgs

    async def _copy_message(self, dst: discord.Thread, h: discord.Message) -> bool:
        """把一則訊息重貼到 dst；使用者的發言加上標記讓 LLM 仍當成使用者說的。回傳是否有貼。"""
        if h.author.id == self.bot_id and not is_copied_user(h.content):
            if not h.content.strip():
                return False
            await dst.send(h.content)                    # 回覆、對話摘要：原樣（含小字用量）
            return True
        if is_copied_user(h.content):                    # 分支再分支：已經是搬過的使用者發言
            head, text = h.content.split("\n", 1)[0], copied_user_text(h.content)
        else:
            name = " ".join(h.author.display_name.replace("*", "").split())[:40] or "使用者"
            head, text = f"{USER_HEAD}{name}**", h.clean_content.strip()
        limit = getattr(dst.guild, "filesize_limit", 10 * 1024 * 1024) or 10 * 1024 * 1024
        files, notes, total = [], [], 0
        for a in h.attachments:
            if len(files) >= 10 or total + a.size > limit:
                notes.append(f"（附件 {a.filename} 太大或太多，沒有搬移）")
                continue
            total += a.size
            files.append(discord.File(io.BytesIO(await a.read()), filename=a.filename))
        body = "\n".join(x for x in [text, *notes] if x)
        if not body and not files:
            return False
        chunks = daily.split_message(body, limit=1990 - len(head)) if body else [""]
        for i, chunk in enumerate(chunks):                # 每一段都帶標記，才不會被當成 LLM 的回覆
            await dst.send(f"{head}\n{chunk}" if chunk else head, files=files if i == 0 else [])
        files.clear()
        return True

    async def branch_thread(self, interaction: discord.Interaction) -> None:
        src = interaction.channel
        if not self._is_chat_thread(src):
            await interaction.response.send_message("這個按鈕只能在 AI 對話的對話串裡使用。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            parent = await self.dc.channel(self.channel_id)
            dst = await parent.create_thread(name=f"🔀 {src.name}"[:100],  # type: ignore[attr-defined]
                                             type=discord.ChannelType.public_thread, auto_archive_duration=10080)
        except discord.HTTPException as e:
            await interaction.edit_original_response(content=f"⚠️ 開對話串失敗：{str(e)[:150]}")
            return
        copied = 0
        try:
            async with self._locks.setdefault(src.id, asyncio.Lock()):      # 等這個對話串正在進行的回覆做完
                async with self._locks.setdefault(dst.id, asyncio.Lock()):
                    msgs = await self._collect(src)
                    for i, h in enumerate(msgs, 1):
                        if await self._copy_message(dst, h):
                            copied += 1
                        if i % 5 == 0:
                            await interaction.edit_original_response(content=f"🔀 搬移中… {i}/{len(msgs)}")
                    await self._send_controls(dst, f"🔀 已從「{src.name}」分支，搬移了 {copied} 則；直接在這裡接著聊。")
        except Exception as e:
            log.exception("分支對話串失敗")
            await interaction.edit_original_response(content=f"⚠️ 分支到一半失敗：{type(e).__name__}：{str(e)[:150]}"
                                                             f"（已搬 {copied} 則：{dst.mention}）")
            return
        await interaction.edit_original_response(content=f"✅ 已分支：{dst.mention}（搬移 {copied} 則）")

    async def ask_delete(self, interaction: discord.Interaction) -> None:
        thread = interaction.channel
        if not self._is_chat_thread(thread):
            await interaction.response.send_message("這個按鈕只能在 AI 對話的對話串裡使用。", ephemeral=True)
            return
        view = ConfirmDeleteView(self, thread, interaction.user.id)       # type: ignore[arg-type]
        await interaction.response.send_message(
            f"{NOTE_PREFIX} ⚠️ 確定要刪除這個對話串（{thread.name}）嗎？刪除後無法復原。", view=view)
        view.message = await interaction.original_response()

    async def delete_thread(self, interaction: discord.Interaction, thread: discord.Thread) -> None:
        await interaction.response.defer()
        try:
            await thread.delete()                         # 對話串刪掉，確認訊息也一起消失
        except discord.Forbidden:
            await interaction.followup.send("⚠️ 刪不掉：機器人缺少「管理貼文（Manage Threads）」權限。", ephemeral=True)
            try:
                await interaction.message.delete()        # type: ignore[union-attr]
            except discord.HTTPException:
                pass
            return
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            await interaction.followup.send(f"⚠️ 刪除失敗：{str(e)[:150]}", ephemeral=True)
            return
        for d in (self._locks, self._starters):
            d.pop(thread.id, None)
        self._auto.discard(thread.id)

    def _wants_title(self, thread: discord.Thread) -> bool:
        return (thread.id in self._auto or is_placeholder(thread.name)) and thread.id not in self._retitling

    async def _retitle(self, thread: discord.Thread, user_text: str, reply: str) -> None:
        """第一輪對話後，請 AI 依內容取標題，改成「日期_標題」。失敗就保留原名，下一輪再試。"""
        self._retitling.add(thread.id)
        try:
            raw = await self.llm.chat(TITLE_PROMPT, data=f"使用者：{user_text[:600]}\n\n回覆：{reply[:600]}",
                                      max_tokens=60, use_memory=False)
            title = clean_title(raw)
            if not title:
                return
            day = thread.name[:10] if is_placeholder(thread.name) else (
                thread.created_at.astimezone(ZoneInfo(self.cfg.tz)).strftime("%Y-%m-%d")
                if thread.created_at else self._today())
            await thread.edit(name=f"{day}_{title}"[:100])
            self._auto.discard(thread.id)
        except Exception:
            log.warning("AI 重新命名對話串失敗：%s", thread.id, exc_info=True)
        finally:
            self._retitling.discard(thread.id)

    # ------------------------------------------------------------ 訊息
    def _in_chat(self, ch) -> bool:
        if getattr(ch, "id", None) == self.channel_id:
            return True
        return isinstance(ch, discord.Thread) and ch.parent_id == self.channel_id

    async def on_message(self, m: discord.Message) -> None:
        if m.author.bot:
            return
        if getattr(m.channel, "id", None) == self.prompt_channel_id:
            await self._on_prompt_channel(m)
            return
        if not self._in_chat(m.channel):
            return
        if m.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        if isinstance(m.channel, discord.Thread):
            thread = m.channel
        else:                                        # 在主頻道發 → 開新的對話串
            thread = await m.create_thread(name=thread_title(m.clean_content,
                                                             m.attachments[0].filename if m.attachments else "新對話"),
                                           auto_archive_duration=10080)
            self._starters[thread.id] = m
            self._auto.add(thread.id)
        lock = self._locks.setdefault(thread.id, asyncio.Lock())
        async with lock:
            if m.clean_content.strip() == COMPACT_CMD:          # 手動壓縮
                try:
                    async with thread.typing():
                        await self.compact(thread, reason="手動")
                except Exception as e:
                    log.exception("壓縮失敗")
                    await thread.send(f"{NOTE_PREFIX} ⚠️ 壓縮失敗：{type(e).__name__}：{str(e)[:200]}")
                return
            try:
                async with thread.typing():
                    reply, user_text, usage, messages = await self.answer(thread, m)
                reply, memos = split_memos(reply)
            except Exception as e:
                log.exception("AI 對話回覆失敗")
                await thread.send(f"{NOTE_PREFIX} ⚠️ 回覆失敗：{type(e).__name__}：{str(e)[:200]}")
                return
            used = self._used(usage, messages, reply)
            ctx = self.cfg.llm_context
            footer = (f"{USAGE_TAG} 上下文 {'約 ' if not usage else ''}{used:,}／{ctx:,} tokens"
                      f"（{100 * used / ctx:.0f}%）")
            chunks = daily.split_message(reply or "（模型沒有回覆內容）", limit=1990 - len(footer) - 1)
            chunks[-1] = f"{chunks[-1]}\n{footer}"
            for chunk in chunks:
                await thread.send(chunk)
            if reply and self._wants_title(thread):
                asyncio.create_task(self._retitle(thread, user_text, reply))
            if used >= ctx * self.cfg.chat_compact_pct / 100:
                try:
                    async with thread.typing():
                        await self.compact(thread, reason=f"自動：用到 {100 * used / ctx:.0f}%",
                                           base=messages + [{"role": "assistant", "content": reply}], before=used)
                except Exception:
                    log.exception("自動壓縮失敗")
        await self._remember(user_text, memos)

    def _used(self, usage: dict, messages: list[dict], reply: str) -> int:
        """這輪之後的 context 用量：有端點回報就用實際值（並校正估算比例），沒有就估。"""
        chars = sum(_text_len(m) for m in messages)
        imgs = sum(1 for m in messages if isinstance(m["content"], list)
                   for p in m["content"] if p.get("type") == "image_url")
        if usage.get("prompt_tokens"):
            if chars:
                self._tpc = min(max((usage["prompt_tokens"] - imgs * IMAGE_TOKENS) / chars, 0.3), 1.6)
            return usage.get("total_tokens") or usage["prompt_tokens"] + usage.get("completion_tokens", 0)
        return int((chars + len(reply)) * self._tpc + imgs * IMAGE_TOKENS)

    def _char_budget(self, system: str, images: int, max_tokens: int) -> int:
        tokens = self.cfg.llm_context - max_tokens - SAFETY_TOKENS - images * IMAGE_TOKENS
        return max(int(tokens / self._tpc) - len(system), 4000)

    async def compact(self, thread: discord.Thread, reason: str, base: list[dict] | None = None,
                      before: int = 0) -> None:
        """把目前為止的對話濃縮成摘要，貼在對話串裡；之後的對話從摘要接著。"""
        if base is None:
            _, _, _, base = await self.answer(thread, None, dry=True)
        msgs = list(base)
        if msgs and msgs[-1]["role"] == "user":
            msgs.append({"role": "assistant", "content": "（好）"})
        msgs.append({"role": "user", "content": prompts.get("CHAT_COMPACT")})
        # 文字進度條：依已產生的 token ÷ 預估長度（摘要約 3,000 字），每 2 秒更新一次
        expect = max(int(3000 * self._tpc), 500)
        state = {"n": 0}
        status = await thread.send(f"{NOTE_PREFIX} 🗜️ 正在壓縮對話（{reason}）… {progress_bar(0)}")
        task = activity.start("壓縮 AI 對話", total=1, background=False)

        def on_token(n: int) -> None:
            state["n"] = n
            task.set_llm(n / expect)

        async def updater() -> None:
            last = ""
            while True:
                await asyncio.sleep(2)
                text = f"{NOTE_PREFIX} 🗜️ 正在壓縮對話（{reason}）… {progress_bar(min(state['n'] / expect, 0.99))}"
                if text != last:
                    try:
                        await status.edit(content=text)
                    except discord.HTTPException:
                        pass
                    last = text
        up = asyncio.create_task(updater())
        try:
            summary, usage = await self.llm.chat_messages(msgs, max_tokens=4000, progress=on_token)
        finally:
            up.cancel()
            activity.finish(task)
            try:
                await status.delete()
            except discord.HTTPException:
                pass
        summary = split_memos(summary)[0].strip() or "（摘要失敗：模型沒有回覆內容）"
        after = int(len(summary) * self._tpc)
        parts = daily.split_message(summary, limit=1800)
        for i, p in enumerate(parts, 1):
            head = f"{SUMMARY_HEAD}（{reason}）**" if i == 1 else f"{SUMMARY_HEAD}（續 {i}/{len(parts)}）**"
            tail = (f"\n-# 🗜️ 壓縮前約 {before:,} tokens → 摘要約 {after:,} tokens；之後從這份摘要接著聊"
                    if i == len(parts) else "")
            await thread.send(f"{head}\n{p}{tail}")
        log.info("🗜️ 對話串 %s 已壓縮（%s）", thread.id, reason)

    async def _on_prompt_channel(self, m: discord.Message) -> None:
        if not self._trusted(m):
            return
        if m.content.startswith(PROMPT_TAG):
            await self.load_prompt()
            await m.add_reaction("✅")
            log.info("AI 對話提示詞已更新為最新一則（v%d）", self.prompt_version)
        elif m.content.startswith(MEM_TAG) and self.memory is not None:
            await self.memory.load()
            await m.add_reaction("✅")

    async def _remember(self, user_text: str, memos: list[str]) -> None:
        """模型標出值得記的事才動作：使用者明確要求 → 馬上升級；否則等對話安靜一陣子再一次整理。"""
        if self.memory is None:
            return
        explicit = bool(EXPLICIT_RE.search(user_text or ""))
        if explicit:
            self.memory.add_correction(user_text)          # 使用者親口說的：當成更正，最優先、不會被刪
        for note in memos:
            self.memory.observe(note, [], "")
        waiting = self._idle_task is not None and not self._idle_task.done() and self._sleeping
        if not memos and not explicit:
            if waiting:                                    # 還在聊 → 重新計時，等真的安靜了再整理
                self._schedule(IDLE_UPGRADE_SEC)
            return
        soon = explicit or bool(SOON_RE.search(user_text or ""))
        log.info("🧠 AI 對話：%s（%d 則）", "馬上整理記憶" if soon else "有值得記的事，對話安靜後整理",
                 len(memos) + (1 if explicit else 0))
        self._schedule(0 if soon else IDLE_UPGRADE_SEC)

    def _schedule(self, delay: float) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            if not self._sleeping:
                return                                     # 正在整理中：這次的會留到下一輪
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._upgrade_later(delay))

    async def _upgrade_later(self, delay: float) -> None:
        self._sleeping = True
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._sleeping = False
        try:
            await self.memory.upgrade("自動：對話中有值得記住的事", urgent=True)   # type: ignore[union-attr]
        except Exception:
            log.exception("AI 對話記憶升級失敗")
        if self.memory is not None and (self.memory.observations or self.memory.corrections):
            self._schedule(IDLE_UPGRADE_SEC)               # 整理期間又有新的 → 稍後再一輪

    # ------------------------------------------------------------ 組對話
    async def _starter(self, thread: discord.Thread) -> discord.Message | None:
        if thread.id in self._starters:
            return self._starters[thread.id]
        try:
            parent = await self.dc.channel(thread.parent_id)
            m = await parent.fetch_message(thread.id)       # 從訊息開的對話串：ID 和開頭訊息相同
            self._starters[thread.id] = m
            return m
        except (discord.NotFound, discord.HTTPException, AttributeError):
            return None

    async def history(self, thread: discord.Thread) -> tuple[str, list[discord.Message]]:
        """回傳 (最近一次壓縮的摘要, 摘要之後的訊息)；沒壓縮過就是 ("", 開頭那則＋整個對話串)。"""
        msgs = await self._collect(thread)
        # 找最後一組摘要（可能分成好幾則）
        idx = [i for i, h in enumerate(msgs) if h.author.id == self.bot_id and h.content.startswith(SUMMARY_HEAD)]
        if not idx:
            return "", msgs
        end = idx[-1]
        start = end
        while start - 1 in idx:
            start -= 1
        summary = "\n".join(_strip_footer(msgs[i].content.split("\n", 1)[1] if "\n" in msgs[i].content else "")
                            for i in range(start, end + 1))
        return summary.strip(), msgs[end + 1:]

    def _is_bot_turn(self, h: discord.Message) -> bool:
        """bot 自己的回覆（分支搬移時由 bot 重貼的使用者發言不算）。"""
        return h.author.id == self.bot_id and not is_copied_user(h.content)

    async def _doc_text(self, a) -> str | None:
        from enrich.document import Unsupported, extract_text, is_supported
        if a.id in self._docs:
            self._docs.move_to_end(a.id)
            return self._docs[a.id]
        if not is_supported(a.filename) or a.size > 40 * 1024 * 1024:
            return None
        try:
            data = await a.read()
            text = await asyncio.to_thread(extract_text, a.filename, data)
        except Unsupported:
            return None
        except Exception as e:
            return f"（讀取失敗：{type(e).__name__}）"
        text = (text or "").strip()
        if len(text) > DOC_CHARS:
            text = text[:DOC_CHARS] + f"\n…（後面還有 {len(text) - DOC_CHARS:,} 字省略）"
        self._docs[a.id] = text
        while len(self._docs) > 50:
            self._docs.popitem(last=False)
        return text

    async def _links(self, text: str) -> list[str]:
        from enrich.link import fetch_text, find_urls
        out = []
        for u in find_urls(text)[:2]:
            try:
                page, kind = await fetch_text(u)
            except Exception as e:
                out.append(f"【連結 {u}】（讀取失敗：{type(e).__name__}）")
                continue
            if page:
                out.append(f"【連結 {u} 的內容】\n{page[:LINK_CHARS]}")
        return out

    async def answer(self, thread: discord.Thread, latest: discord.Message | None,
                     dry: bool = False) -> tuple[str, str, dict, list[dict]]:
        """組好「提示詞＋記憶＋摘要＋歷史」交給模型。回傳 (回覆, 使用者這次說的話, 用量, 送出的 messages)。
        dry=True 只組 messages、不呼叫模型（手動壓縮用）。"""
        summary, msgs = await self.history(thread)
        if latest is not None and latest.id not in {x.id for x in msgs}:
            msgs.append(latest)
        latest_id = latest.id if latest is not None else (msgs[-1].id if msgs else 0)
        # 哪些圖片要以圖片交給模型：最近 MAX_IMAGES 張
        image_ids: list[int] = []
        if self.llm.image_support:
            for h in reversed(msgs):
                if self._is_bot_turn(h):
                    continue
                for a in reversed(h.attachments):
                    if _is_image(a) and a.size <= MAX_IMAGE_BYTES and len(image_ids) < MAX_IMAGES:
                        image_ids.append(a.id)
        thinking = False
        user_text = ""
        turns: list[dict] = []
        for h in msgs:
            if self._is_bot_turn(h):
                body = _strip_footer(h.content)
                if body.strip():
                    turns.append({"role": "assistant", "content": body})
                continue
            text = copied_user_text(h.content).strip() if is_copied_user(h.content) else h.clean_content.strip()
            if h.id == latest_id and not dry:
                if text.startswith(THINK_PREFIX):
                    thinking, text = True, text[len(THINK_PREFIX):].strip()
                user_text = text
            elif text.startswith(THINK_PREFIX):
                text = text[len(THINK_PREFIX):].strip()
            if text == COMPACT_CMD:
                continue
            pieces: list[str] = [text] if text else []
            images: list[dict] = []
            for a in h.attachments:
                if _is_image(a):
                    if a.id in image_ids:
                        data = await a.read()
                        b64 = base64.b64encode(data).decode()
                        images.append({"type": "image_url", "image_url": {"url": f"data:{_mime(a)};base64,{b64}"}})
                        pieces.append(f"（附圖：{a.filename}）")
                    else:
                        pieces.append(f"（附圖：{a.filename}，較早的圖片，這裡只附檔名）")
                    continue
                doc = await self._doc_text(a)
                if doc is None:
                    pieces.append(f"（附件：{a.filename}，這種檔案讀不到內容）")
                else:
                    pieces.append(f"【附件 {a.filename} 的內容】\n{doc}")
            if h.id == latest_id and text and not dry:
                pieces += await self._links(text)
            body = "\n\n".join(pieces) or "（空白訊息）"
            turns.append({"role": "user", "content": [{"type": "text", "text": body}] + images if images else body})
        max_tokens = 8000 if thinking else 4000
        system = self.system_prompt(summary)
        turns = trim_history(merge_roles(turns), limit=self._char_budget(system, len(image_ids), max_tokens))
        if turns and turns[0]["role"] == "assistant":
            turns.insert(0, {"role": "user", "content": "（對話開始）"})
        messages = [{"role": "system", "content": system}] + turns
        if dry:
            return "", "", {}, messages
        log.info("💬 AI 對話：對話串 %s，%d 則歷史%s%s", thread.id, len(turns), "（有摘要）" if summary else "",
                 "（思考模式）" if thinking else "")
        reply, usage = await self.llm.chat_messages(messages, max_tokens=max_tokens, thinking=thinking)
        return reply, user_text, usage, messages
