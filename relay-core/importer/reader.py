# relay-core/importer/reader.py
"""串流解析 Telegram Desktop 匯出的 result.json（第 9.1、9.2 節）。

用 ijson 一次只讀一則訊息，幾百 MB 的匯出檔也不會整份載入記憶體。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

import ijson

NOT_INCLUDED_PREFIX = "(File "  # "(File not included…)" / "(File exceeds maximum size…)"


@dataclass
class ChatMeta:
    name: str
    type: str          # private_group / private_supergroup / public_supergroup / …
    export_id: int
    bot_chat_id: int   # 換算成 Bot API 的 chat_id


@dataclass
class HistMsg:
    tg_msg: int
    dt: datetime                 # 帶時區（UTC）
    text: str
    type: str                    # text | image | file | link | voice | video | sticker
    rel_path: str | None = None  # 匯出資料夾內的相對路徑
    file_name: str | None = None
    missing_reason: str | None = None  # 附件沒有匯出時的原因


def to_bot_chat_id(chat_type: str, export_id: int) -> int:
    """一般群組：-id；超級群組／頻道：-100id。"""
    if chat_type in ("private_group",):
        return -export_id
    if chat_type in ("private_supergroup", "public_supergroup", "private_channel", "public_channel"):
        return int(f"-100{export_id}")
    return export_id  # 私人對話等，照原樣


def read_meta(result_json: Path) -> ChatMeta:
    """只讀檔頭的 name/type/id，遇到 messages 就停。"""
    meta: dict = {}
    with open(result_json, "rb") as f:
        for prefix, event, value in ijson.parse(f):
            if prefix in ("name", "type", "id") and event in ("string", "number"):
                meta[prefix] = value
            if prefix == "messages" and event == "start_array":
                break
    if "id" not in meta or "type" not in meta:
        raise ValueError("result.json 不是單一聊天的匯出檔（找不到 id/type）")
    export_id = int(meta["id"])
    return ChatMeta(str(meta.get("name", "")), str(meta["type"]), export_id,
                    to_bot_chat_id(str(meta["type"]), export_id))


def _plain_text(m: dict) -> tuple[str, bool]:
    """回傳 (文字, 是否含連結)。text 可能是字串或「字串/物件」混合的陣列。"""
    ents = m.get("text_entities")
    if isinstance(ents, list) and ents:
        text = "".join(str(e.get("text", "")) for e in ents if isinstance(e, dict))
        has_link = any(isinstance(e, dict) and e.get("type") in ("link", "text_link") for e in ents)
        return text, has_link
    t = m.get("text", "")
    if isinstance(t, str):
        return t, False
    parts, has_link = [], False
    for p in t if isinstance(t, list) else []:
        if isinstance(p, str):
            parts.append(p)
        elif isinstance(p, dict):
            parts.append(str(p.get("text", "")))
            has_link = has_link or p.get("type") in ("link", "text_link")
    return "".join(parts), has_link


def _dt(m: dict, tz: str) -> datetime:
    if m.get("date_unixtime"):
        return datetime.fromtimestamp(int(m["date_unixtime"]), tz=timezone.utc)
    # 舊版匯出只有本地時間字串，視為設定的時區
    return datetime.fromisoformat(m["date"]).replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def convert(m: dict, tz: str) -> HistMsg | None:
    """把一則匯出訊息轉成 HistMsg；服務訊息或空訊息回傳 None。"""
    if m.get("type") != "message":
        return None
    mid = int(m["id"])
    dt = _dt(m, tz)
    text, has_link = _plain_text(m)

    def attach(kind: str, path: str | None, name: str | None) -> HistMsg:
        missing = None
        rel = path
        if path is None or str(path).startswith(NOT_INCLUDED_PREFIX):
            missing = "未匯出" if path is None or "not included" in str(path) else "過大未匯出"
            rel = None
        fname = name or (os.path.basename(path) if rel else None) or f"file_{mid}"
        return HistMsg(mid, dt, text, kind, rel, fname, missing)

    if "photo" in m:
        return attach("image", m.get("photo"), None if m.get("photo", "").startswith(NOT_INCLUDED_PREFIX)
                      else os.path.basename(m["photo"]))
    media = m.get("media_type")
    if media == "sticker":
        emoji = m.get("sticker_emoji") or ""
        return HistMsg(mid, dt, text or f"[貼圖 {emoji}]".replace(" ]", "]"), "sticker")
    if "file" in m:
        kind = {"video_file": "video", "animation": "video", "video_message": "video",
                "voice_message": "voice", "audio_file": "file"}.get(media or "", None)
        if kind is None:
            kind = "image" if str(m.get("mime_type", "")).startswith("image/") else "file"
        return attach(kind, m.get("file"), m.get("file_name"))
    if "location_information" in m:
        li = m["location_information"]
        return HistMsg(mid, dt, text or f"📍 位置 {li.get('latitude', 0):.5f}, {li.get('longitude', 0):.5f}", "text")
    if "contact_information" in m:
        ci = m["contact_information"]
        name = f"{ci.get('first_name', '')} {ci.get('last_name', '')}".strip()
        return HistMsg(mid, dt, text or f"👤 聯絡人 {name}", "text")
    if "poll" in m:
        return HistMsg(mid, dt, text or f"📊 投票：{m['poll'].get('question', '')}", "text")
    if text.strip():
        return HistMsg(mid, dt, text, "link" if has_link else "text")
    return None


def iter_messages(result_json: Path, tz: str) -> Iterator[HistMsg]:
    with open(result_json, "rb") as f:
        for m in ijson.items(f, "messages.item", use_float=True):
            h = convert(m, tz)
            if h is not None:
                yield h
