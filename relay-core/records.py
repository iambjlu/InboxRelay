# relay-core/records.py
"""#關鍵字庫 紀錄格式（第 7.5 節）。純函式，可單元測試。"""
from __future__ import annotations

from dataclasses import dataclass

PREFIX = "KW|v2"
OCR_NL = " ⏎ "       # 舊版圖中文字的換行寫法（讀取時相容；新版寫成空白）
OCR_PREFIX = "OCR|"     # 圖中文字訊息：OCR|tg_chat|tg_msg（一張圖一則）
OCR_REF = "見 OCR 訊息"   # 主紀錄 ocr 欄位：文字在 OCR 訊息裡
OCR_MARKERS = ("（無文字）", "（無法取得圖片）")
OCR_ROOM = 1960          # 一則 OCR 訊息最多放多少字（2000 扣掉標記行）
MAX_LEN = 2000


@dataclass
class KwRecord:
    tg_chat: int
    tg_msg: int
    origin: str          # live | import | dc（在 Discord 發、反向同步到 Telegram）
    date: str            # YYYY-MM-DD HH:MM（設定時區）
    type: str            # text | image | album | file | link | voice | video | sticker
    mirror: str          # 鏡像訊息的 Discord 連結
    src: str | None = None
    filename: str | None = None
    fid: str | None = None       # Telegram file_unique_id（編輯同步判斷附件是否被換掉）
    reply: str | None = None     # AI 摘要回覆的 Discord 連結（刪除同步時一併刪）
    shot: str | None = None      # 連結截圖貼文的 Discord 連結（可多個，以空白分隔）
    kw: str | None = None
    gist: str | None = None
    excerpt: str | None = None
    enriched: bool = False
    ocr: str | None = None       # 圖中文字（本機 ANE 辨識）；「（無文字）」等標記代表做過了
    img: str | None = None       # 文字裡「連到圖片」的網址（以空白分隔）：也做圖中文字辨識

    @property
    def key(self) -> tuple[int, int]:
        return (self.tg_chat, self.tg_msg)


def one_line(text: str) -> str:
    return " ".join(text.split())


def make_excerpt(text: str, n: int = 200) -> str:
    t = one_line(text)
    return t if len(t) <= n else t[: n - 1] + "…"


def flat_ocr(ocr: str | None) -> str:
    """圖中文字寫進紀錄時攤平成一行（行與行之間用空白），搜尋不受影響。"""
    if not ocr:
        return ""
    return " ".join(" ".join(x.split()) for x in ocr.replace(OCR_NL, "\n").splitlines() if x.strip())


def _head_lines(r: KwRecord) -> list[str]:
    lines = [
        PREFIX,
        f"tg_chat: {r.tg_chat}",
        f"tg_msg: {r.tg_msg}",
        f"origin: {r.origin}",
        f"date: {r.date}",
        f"type: {r.type}",
        f"mirror: {r.mirror}",
    ]
    for k in ("src", "filename", "fid", "reply", "shot", "img", "kw", "gist"):
        v = getattr(r, k)
        if v:
            lines.append(f"{k}: {one_line(v)}")
    return lines


def ocr_message(r: KwRecord) -> str | None:
    """這張圖的 OCR 訊息（#關鍵字庫 裡一張圖一則）：第一行是標記，其餘塞滿圖中文字。沒有文字回傳 None。"""
    text = flat_ocr(r.ocr)
    if not text or text in OCR_MARKERS or text == OCR_REF:
        return None
    head = f"{OCR_PREFIX}{r.tg_chat}|{r.tg_msg}\n"
    return head + text[: MAX_LEN - len(head)]


def parse_ocr_message(content: str) -> tuple[tuple[int, int], str] | None:
    if not content.startswith(OCR_PREFIX):
        return None
    head, _, text = content.partition("\n")
    try:
        chat, msg = head[len(OCR_PREFIX):].split("|")[:2]
        return (int(chat), int(msg)), text
    except ValueError:
        return None


def format_kw(r: KwRecord) -> str:
    lines = _head_lines(r)
    end = f"enriched: {'yes' if r.enriched else 'no'}"
    if r.ocr:
        # 主紀錄只記「做過了」：真正的文字放在這張圖自己的 OCR 訊息（一則塞滿）
        lines.append(f"ocr: {r.ocr if r.ocr in OCR_MARKERS else OCR_REF}")
    if r.excerpt:
        room = MAX_LEN - sum(len(x) + 1 for x in lines) - len(end) - len("excerpt: ") - 1
        ex = one_line(r.excerpt)
        if room <= 0:
            ex = ""
        elif len(ex) > room:
            ex = ex[: room - 1] + "…"
        if ex:
            lines.append(f"excerpt: {ex}")
    lines.append(end)
    return "\n".join(lines)[:MAX_LEN]


def parse_kw(content: str) -> dict[str, str] | None:
    """解析一則 #關鍵字庫 訊息；不是 KW 紀錄就回傳 None。"""
    if not content.startswith(PREFIX):
        return None
    out: dict[str, str] = {}
    for line in content.split("\n")[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
    if "tg_chat" not in out or "tg_msg" not in out:
        return None
    return out


def parse_key(content: str) -> tuple[int, int] | None:
    d = parse_kw(content)
    if not d:
        return None
    try:
        return (int(d["tg_chat"]), int(d["tg_msg"]))
    except ValueError:
        return None


def from_dict(d: dict[str, str]) -> KwRecord:
    return KwRecord(
        tg_chat=int(d["tg_chat"]),
        tg_msg=int(d["tg_msg"]),
        origin=d.get("origin", "live"),
        date=d.get("date", ""),
        type=d.get("type", "text"),
        mirror=d.get("mirror", ""),
        src=d.get("src") or None,
        filename=d.get("filename") or None,
        fid=d.get("fid") or None,
        reply=d.get("reply") or None,
        shot=d.get("shot") or None,
        img=d.get("img") or None,
        kw=d.get("kw") or None,
        gist=d.get("gist") or None,
        excerpt=d.get("excerpt") or None,
        ocr=(d.get("ocr") or "").replace(OCR_NL, " ") or None,
        enriched=d.get("enriched", "no") == "yes",
    )


def parse_record(content: str) -> KwRecord | None:
    d = parse_kw(content)
    if not d:
        return None
    try:
        return from_dict(d)
    except (KeyError, ValueError):
        return None


def parse_message_url(url: str) -> tuple[int, int] | None:
    """https://discord.com/channels/<guild>/<channel>/<message> → (channel, message)。"""
    parts = url.rstrip("/").split("/")
    try:
        return int(parts[-2]), int(parts[-1])
    except (IndexError, ValueError):
        return None
