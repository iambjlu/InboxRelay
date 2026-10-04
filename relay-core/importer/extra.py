# relay-core/importer/extra.py
"""補充資料匯入（第 9.8 節）：發票、筆記等「不是 Telegram 訊息」的素材。

支援的格式（專案裡只放通用格式；個人特殊格式請先轉成 JSONL，轉換腳本不要放進專案）：
- 財政部電子發票整合服務平台下載的 CSV（表頭=M／明細=D）
- 通用 JSONL（見下方 parse_jsonl）

設計：
- 全部放在同一個 #補充資料 頻道（之後新增格式也不用再開頻道），一天一種素材一則，例如
  「🧾 2026-05-03-Sunday 消費 6 筆｜NT$822」「📒 2019-05-03-Friday 筆記」。
- 每則也寫進 #關鍵字庫（origin=extra，tg_chat=0），所以每日／月／年總結、關鍵字、搜尋都會用到。
- 金額由程式計算，AI 只做關鍵字與重點（在夜間時段）。
- 去重／更新：同一天同一種素材的鍵固定；內容沒變就跳過，變了（例如新匯出檔多了發票）就換掉舊的。
- 匯入後把有變動的日期標成「需要重新總結」，03:30 那一輪會重寫日／月／年總結。

新增通用格式：寫 parse_xxx → list[DayDoc]，在 scan() 裡依副檔名／內容判斷呼叫。
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import formatting

EXTRA_CHAT = 0            # 補充資料的 tg_chat（Telegram 不會有 0 這個聊天 ID）
POST_LIMIT = 1950
EXCERPT_LIMIT = 1500      # 寫進 #關鍵字庫 的內容（AI 分析、總結、搜尋都靠它）
KIND_META = {             # 內建種類 → (圖示, 名稱, 紀錄時間)；JSONL 的種類自己帶圖示與名稱
    "invoice": ("🧾", "消費", "23:59"),
}
DEFAULT_TIME = "23:58"


@dataclass
class DayDoc:
    kind: str                 # invoice / JSONL 自訂的種類
    day: str                  # YYYY-MM-DD
    title: str                # 標題後半，例如「消費 6 筆｜NT$822」
    body: str                 # 貼文內文（可以很長，會自動拆）
    excerpt: str              # 給 AI／總結／搜尋的精簡內容
    sources: set[str] = field(default_factory=set)
    icon: str = ""
    name: str = ""

    def meta(self) -> tuple[str, str, str]:
        icon, name, hhmm = KIND_META.get(self.kind, ("📎", self.kind, DEFAULT_TIME))
        return self.icon or icon, self.name or name, hhmm

    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(f"{self.title}\n{self.body}".encode()).hexdigest()[:16]


def doc_key(kind: str, day: str, part: int) -> tuple[int, int]:
    h = hashlib.sha1(f"{kind}|{day}|{part}".encode()).hexdigest()
    return EXTRA_CHAT, int(h[:15], 16)


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp950", "big5hkscs"):
        try:
            t = raw.decode(enc)
            if enc == "utf-16" and not raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return t
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ------------------------------------------------------------------ 發票（財政部電子發票整合服務平台 CSV）
def is_invoice_csv(text: str) -> bool:
    head = text.lstrip("\ufeff")[:200]
    return head.startswith("表頭=M") or ("發票號碼" in head and "商店店名" in head)


_CORP = re.compile(r"(股份有限公司|有限公司|企業社|商行)")


def short_store(name: str) -> str:
    n = _CORP.sub("", name).strip()
    return n or name


def parse_invoice_text(text: str, source: str = "") -> dict[str, dict]:
    """回傳 發票號碼 → {day, store, total, status, items:[(名稱, 小計)]}。"""
    invs: dict[str, dict] = {}
    text = text.lstrip("\ufeff")
    first = text.split("\n", 1)[0]
    delim = "|" if first.count("|") > first.count(",") else ","     # 平台有「,」與「|」兩種下載格式
    for row in csv.reader(io.StringIO(text), delimiter=delim):
        while row and row[-1].strip() == "":
            row.pop()
        if not row:
            continue
        tag = row[0].strip()
        if tag == "M" and len(row) >= 9:
            day_raw, status, total, no = row[3].strip(), row[-1].strip(), row[-2].strip(), row[-3].strip()
            store = ",".join(row[5:-3]).strip()
            try:
                d = datetime.strptime(day_raw, "%Y%m%d").strftime("%Y-%m-%d")
                amount = int(float(total))
            except ValueError:
                continue
            inv = invs.setdefault(no, {"items": []})
            inv.update(day=d, store=store, total=amount, status=status, source=source)
        elif tag == "D" and len(row) >= 4:
            no = row[1].strip()
            try:
                sub = int(float(row[2]))
            except ValueError:
                continue
            name = ",".join(row[3:]).strip()
            invs.setdefault(no, {"items": []})["items"].append((name, sub))
    return invs


def invoice_docs(invs: dict[str, dict]) -> list[DayDoc]:
    by_day: dict[str, list] = defaultdict(list)
    for no, inv in invs.items():
        if "day" not in inv or "作廢" in inv.get("status", ""):
            continue
        by_day[inv["day"]].append((no, inv))
    docs = []
    for day, lst in sorted(by_day.items()):
        lst.sort(key=lambda x: x[0])
        total = sum(inv["total"] for _, inv in lst)
        lines, ex_parts = [], []
        for no, inv in lst:
            items = [n for n, s in inv["items"] if s > 0]
            store = short_store(inv["store"])
            lines.append(f"- **{store}** NT${inv['total']:,}")
            lines += [f"  - {n}｜{s:+,}" if s < 0 else f"  - {n}｜{s:,}" for n, s in inv["items"] if s != 0]
            ex_parts.append(f"{store} {inv['total']}元（{'、'.join(items[:6])}{'…' if len(items) > 6 else ''}）")
        title = f"消費 {len(lst)} 筆｜NT${total:,}"
        excerpt = f"消費 {len(lst)} 筆共 {total} 元：" + "；".join(ex_parts)
        docs.append(DayDoc("invoice", day, title, "\n".join(lines), excerpt[:EXCERPT_LIMIT],
                           {inv.get("source", "") for _, inv in lst}))
    return docs


# ------------------------------------------------------------------ 通用 JSONL（其他格式先自行轉成這個）
# 一行一筆：{"day": "2019-05-03", "kind": "note", "icon": "📒", "name": "筆記", "body": "內文…",
#            "title": "（選填，預設＝名稱）", "excerpt": "（選填，預設＝內文前 1500 字）"}
# 同一天同一種 kind 的多筆會合併成一則。
def parse_jsonl(text: str, source: str = "") -> list[dict]:
    import json
    rows = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
            date.fromisoformat(str(d["day"]))
        except (ValueError, KeyError, TypeError):
            continue
        if str(d.get("body", "")).strip():
            d["source"] = source
            rows.append(d)
    return rows


def jsonl_docs(rows: list[dict]) -> list[DayDoc]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(str(r.get("kind") or "note"), str(r["day"]))].append(r)
    docs = []
    for (kind, day), lst in sorted(groups.items(), key=lambda x: (x[0][1], x[0][0])):
        first = lst[0]
        body = "\n\n".join(str(r["body"]).strip() for r in lst)
        excerpt = "\n".join(str(r.get("excerpt") or r["body"]).strip() for r in lst)
        name = str(first.get("name") or kind)
        title = str(first.get("title") or f"{name}｜{len(body):,} 字")
        doc = DayDoc(kind, day, title, body, excerpt[:EXCERPT_LIMIT], {r.get("source", "") for r in lst})
        doc.icon, doc.name = str(first.get("icon") or "📎"), name
        docs.append(doc)
    return docs


# ------------------------------------------------------------------ 掃描資料夾
@dataclass
class ScanResult:
    docs: list[DayDoc] = field(default_factory=list)
    used: dict[str, int] = field(default_factory=dict)      # 種類 → 檔案數
    skipped: list[str] = field(default_factory=list)        # 不認得的檔案
    undated: list[str] = field(default_factory=list)        # 格式對但沒有可用的資料


SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def scan(path: Path, date_from: str | None = None, date_to: str | None = None) -> ScanResult:
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    res = ScanResult()
    invs: dict[str, dict] = {}
    rows: list[dict] = []
    for f in files:
        if f.name in SKIP_NAMES or f.name.startswith("._"):
            continue
        rel = str(f.relative_to(path)) if path.is_dir() else f.name
        ext = f.suffix.lower()
        if ext == ".csv":
            text = read_text(f)
            if is_invoice_csv(text):
                invs.update(parse_invoice_text(text, rel))
                res.used["invoice"] = res.used.get("invoice", 0) + 1
                continue
        elif ext == ".jsonl":
            got = parse_jsonl(read_text(f), rel)
            if got:
                rows += got
                for k in {str(r.get("kind") or "note") for r in got}:
                    res.used[k] = res.used.get(k, 0) + 1
            else:
                res.undated.append(rel)
            continue
        res.skipped.append(rel)
    docs = invoice_docs(invs) + jsonl_docs(rows)
    res.docs = [d for d in docs if (not date_from or d.day >= date_from) and (not date_to or d.day <= date_to)]
    res.docs.sort(key=lambda d: (d.day, d.kind))
    return res


def post_chunks(doc: DayDoc) -> list[str]:
    icon, name, _ = doc.meta()
    head = f"## {icon} {formatting.display_date(doc.day)} {doc.title}"
    text = f"{head}\n{doc.body}"
    chunks, cur = [], ""
    for ln in text.split("\n"):
        while len(ln) > POST_LIMIT:                       # 超長的一行硬切
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(ln[:POST_LIMIT])
            ln = ln[POST_LIMIT:]
        if len(cur) + len(ln) + 1 > POST_LIMIT:
            chunks.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        chunks.append(cur)
    if len(chunks) > 1:
        chunks = [c if i == 0 else f"-# {icon} {doc.day} {name}（{i + 1}/{len(chunks)}）\n{c}"
                  for i, c in enumerate(chunks)]
    return chunks
