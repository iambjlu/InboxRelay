# relay-core/recap/daily.py
"""每日總結的材料與 prompt（第 7.7 節）。M5 正式排程與 AI 測試頻道的預覽共用。"""
from __future__ import annotations

import re
from dataclasses import dataclass

import prompts

MAX_ITEM_CHARS = 400
MAX_MATERIAL_CHARS = 24_000

@dataclass
class Item:
    time: str          # HH:MM
    kind: str          # 文字／圖片／檔案／連結…
    text: str          # 原文或摘要
    url: str           # 出處（鏡像訊息連結）


def build_material(items: list[Item], per_item: int | None = None, max_chars: int = MAX_MATERIAL_CHARS) -> str:
    """per_item=None 時自動分配：則數少就每則給多一點字（最多 2000），則數多就壓到 400。"""
    if per_item is None:
        per_item = max(MAX_ITEM_CHARS, min(2000, max_chars // max(len(items), 1)))
    lines, total = [], 0
    for i, it in enumerate(items, 1):
        t = " ".join(it.text.split())
        if len(t) > per_item:
            t = t[:per_item] + "…"
        line = f"[{i}] {it.time}｜{it.kind}｜{t}"
        if total + len(line) > max_chars:
            lines.append(f"（其餘 {len(items) - i + 1} 則因長度省略）")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def build_prompt(date: str) -> str:
    return prompts.render("RECAP_DAILY", date=date)


_CITE_RE = re.compile(r"\[(\d+(?:\s*[,，、]\s*\d+)*)\]")


def link_citations(text: str, urls: list[str]) -> str:
    """把 [3]、[2,5] 換成可點的出處連結；超出範圍的編號直接移除。"""
    def repl(m: re.Match) -> str:
        nums = [int(x) for x in re.split(r"\s*[,，、]\s*", m.group(1))]
        links = [f"[↗{n}](<{urls[n - 1]}>)" for n in nums if 1 <= n <= len(urls)]
        return "".join(links)
    return _CITE_RE.sub(repl, text)


def split_message(text: str, limit: int = 2000) -> list[str]:
    """依換行切成多則，每則 ≤ limit。"""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out
