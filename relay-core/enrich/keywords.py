# relay-core/enrich/keywords.py
"""關鍵字抽取：回傳 (關鍵字列表, 一句話重點)。短訊息也照樣分析（例如「編輯報價單」是待辦）。"""
from __future__ import annotations

import json
import re

import prompts
from llm import LLM


def parse(text: str) -> tuple[list[str], str]:
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            kw = [str(x).strip() for x in d.get("kw", []) if str(x).strip()][:8]
            return kw, str(d.get("gist", "")).strip()
        except (json.JSONDecodeError, AttributeError):
            pass
    return [], text.strip()[:80]


def build_data(material: str, context: list[str] | None = None) -> str:
    """本則訊息＋前後文（前後文只幫助理解，不抽關鍵字）。"""
    parts = [f"【本則訊息】\n{material[:6000]}"]
    if context:
        ctx = "\n".join(f"- {c[:200]}" for c in context[-5:] if c.strip())
        if ctx:
            parts.append(f"【前後文（僅供理解）】\n{ctx}")
    return "\n\n".join(parts)


_FAIL_LINE = re.compile(r"無法讀取|ConnectError|Timeout|HTTPStatusError|Error\)?$")
_NOISE_KW = re.compile(r"^(無法讀取|讀取失敗|連結失效|.*Error|.*Exception|Timeout)$", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[\s　·・,，、\-_/]+", "", s).lower()


def clean_material(material: str) -> str:
    """把「無法讀取（ConnectError）」這種錯誤行拿掉，換成中性說明，避免錯誤字串變成關鍵字。"""
    lines, failed = [], False
    for ln in material.splitlines():
        if _FAIL_LINE.search(ln.strip()) and len(ln.strip()) <= 40:
            failed = True
            continue
        lines.append(ln)
    out = "\n".join(lines).strip()
    if failed:
        out += "\n（連結內容讀不到，請只根據網址本身的網域、路徑、檔名判斷）"
    return out


def filter_keywords(kw: list[str], gist: str, material: str, context: list[str] | None) -> tuple[list[str], str]:
    """關鍵字只能來自本則：本則沒出現、但前後文有出現的詞一律丟掉（防止模型抄上一則）。"""
    mat = _norm(material)
    ctx = _norm("\n".join(context or []))
    keep: list[str] = []
    for k in kw:
        n = _norm(k)
        if not n or _NOISE_KW.match(k.strip()):
            continue
        if n not in mat and n in ctx:
            continue
        if n not in {_norm(x) for x in keep}:
            keep.append(k)
    gist = re.sub(r"^意義不明[:：]\s*原文$", "意義不明", gist.strip())
    return keep, gist


async def extract_keywords(llm: LLM, material: str, context: list[str] | None = None) -> tuple[list[str], str]:
    material = clean_material(material)
    out = await llm.chat(prompts.render("KEYWORDS") + "\n" + prompts.PERSON_RULE, build_data(material, context), max_tokens=300)
    kw, gist = parse(out)
    return filter_keywords(kw, gist, material, context)
