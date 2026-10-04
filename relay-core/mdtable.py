# relay-core/mdtable.py
"""Discord 不支援 Markdown 表格：把模型回的表格轉成條列（程式碼區塊裡的不動）。

| 項目 | 價格 | 備註 |        - 蘋果：價格 30；備註 便宜
|------|------|------|   →    - 香蕉：價格 20
| 蘋果 | 30   | 便宜 |
| 香蕉 | 20   |      |
兩欄的表格寫成「- 第一欄：第二欄」；三欄以上第一欄當標題，其餘欄寫成「表頭 值」（空格子略過）。
"""
from __future__ import annotations

import re

_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    out = []
    for c in re.split(r"(?<!\\)\|", s):
        c = c.replace("\\|", "|").strip()
        if len(c) >= 4 and c.startswith("**") and c.endswith("**"):
            c = c[2:-2].strip()
        out.append(c)
    return out


def _is_table_start(lines: list[str], i: int) -> bool:
    return (i + 1 < len(lines) and "|" in lines[i] and "|" in lines[i + 1] and bool(_SEP.match(lines[i + 1]))
            and len(_cells(lines[i])) >= 2)


def _bullets(header: list[str], rows: list[list[str]]) -> list[str]:
    out = []
    for r in rows:
        r = r + [""] * (len(header) - len(r))
        lead = r[0] or (header[0] if header else "")
        rest = [(header[j] if j < len(header) else "", v) for j, v in enumerate(r[1:], 1) if v]
        if not rest:
            out.append(f"- {lead}")
        elif len(header) == 2:
            out.append(f"- {lead}：{rest[0][1]}")
        else:
            out.append(f"- {lead}：" + "；".join(f"{h} {v}".strip() for h, v in rest))
    return out


def tables_to_bullets(text: str) -> str:
    if not text or "|" not in text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            in_code = not in_code
            out.append(ln)
            i += 1
            continue
        if not in_code and _is_table_start(lines, i):
            header = _cells(lines[i])
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            out.extend(_bullets(header, rows))
            continue
        out.append(ln)
        i += 1
    return "\n".join(out)
