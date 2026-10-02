# relay-core/formatting.py
"""第 4 節訊息格式。純函式、不依賴第三方套件，方便單元測試。"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

DISCORD_LIMIT = 2000
TRUNC_SUFFIX = "…（已截斷）"
# 標頭：「# 2026-10-02-Friday」（舊格式「# 2026-10-02」也認得）
_HEADER_RE = re.compile(r"^(?:-# |# )(\d{4}-\d{2}-\d{2})(?:-[A-Za-z]+)?$")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def display_date(date_str: str) -> str:
    """2026-10-02 → 2026-10-02-Friday（星期固定用英文，不受系統語系影響）。"""
    try:
        return f"{date_str}-{WEEKDAYS[datetime.strptime(date_str, '%Y-%m-%d').weekday()]}"
    except ValueError:
        return f"{date_str}-Wednesday"     # 估長度用（最長的星期名稱）


def local_dt(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(ZoneInfo(tz))


def local_date(dt: datetime, tz: str) -> str:
    """Telegram 給的是 UTC 時間，轉成指定時區的日期字串。"""
    return local_dt(dt, tz).strftime("%Y-%m-%d")


def local_minute(dt: datetime, tz: str) -> str:
    return local_dt(dt, tz).strftime("%Y-%m-%d %H:%M")


def header(date_str: str, style: str = "large") -> str:
    # Discord 標題語法要求 # 後面有空格
    d = display_date(date_str)
    return f"# {d}" if style == "large" else f"-# {d}"


def truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    keep = max(limit - len(TRUNC_SUFFIX), 0)
    return text[:keep] + TRUNC_SUFFIX


def supergroup_src_url(chat_id: int, message_id: int) -> str | None:
    """只有超級群組（-100 前綴）才有 t.me/c 連結；一般群組回傳 None。"""
    s = str(chat_id)
    if not s.startswith("-100"):
        return None
    return f"https://t.me/c/{s[4:]}/{message_id}"


def parse_header_date(content: str) -> str | None:
    """若訊息第一行是日期標頭（# 或 -#），回傳日期字串。"""
    first = content.split("\n", 1)[0].strip()
    m = _HEADER_RE.match(first)
    return m.group(1) if m else None


def oversize_line(name: str) -> str:
    return f"📎 {name}（過大未搬移）"


def build_mirror_content(
    date_str: str,
    body: str,
    oversize_names: list[str] | None = None,
    src_url: str | None = None,
    style: str = "large",
    with_header: bool = True,
    extra_lines: list[str] | None = None,
) -> str:
    """組出一則鏡像訊息（≤ 2000 字）。各段以換行相接：

    # 2026-10-01            ← 只有當天第一則
    訊息內容（會被截斷的只有這段）
    📎 檔名（過大未搬移）    ← 有過大附件才有
    -# [src](<…>)          ← 只有超級群組
    """
    head = header(date_str, style) if with_header else ""
    lines = [oversize_line(n) for n in (oversize_names or [])] + list(extra_lines or [])
    extra = "\n".join(lines)
    extra = truncate(extra, DISCORD_LIMIT // 2)  # 極端情況：大量過大附件
    # 角括號包住網址可避免 Discord 產生連結預覽
    tail = f"-# [src](<{src_url}>)" if src_url else ""

    fixed = [seg for seg in (head, extra, tail) if seg]
    if body:
        room = DISCORD_LIMIT - sum(len(x) for x in fixed) - len(fixed)  # 每段之間一個換行
        body = truncate(body, room)
    segments = [seg for seg in (head, body, extra, tail) if seg]
    return "\n".join(segments)


def build_live_message(
    date_str: str,
    body: str,
    src_url: str | None = None,
    style: str = "large",
    with_header: bool = True,
) -> str:
    """純文字實時訊息（M1 介面，保留相容）。"""
    return build_mirror_content(date_str, body, None, src_url, style, with_header)
