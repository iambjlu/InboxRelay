# relay-core/search/engine.py
"""自然語言搜尋（只用 Qwen3.6，不額外載模型）。

1. Qwen 解析問題 → 起訖日期＋關鍵詞（含同義詞，例如「吃」→ 餐、便當、餐廳）。
2. 程式在記憶體裡篩選候選：#關鍵字庫 紀錄（原文節錄、檔名、關鍵字、重點）＋ #總結 的每日總結。
3. Qwen 從候選中挑出相關的並回答，附出處。
回覆格式（第 7.9 節）：每筆一定有日期，不出現任何訊息 ID。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

import prompts
from ocr import useful
from recap import daily

WEEKDAYS = "一二三四五六日"
TYPE_ICON = {"analysis": "🤖 AI 分析", "text": "💬 文字", "image": "🖼️ 圖片", "album": "🖼️ 相簿", "file": "📄 檔案", "link": "🔗 連結",
             "voice": "🎤 語音", "video": "🎬 影片", "sticker": "😀 貼圖", "daily": "📰 每日總結",
             "invoice": "🧾 消費"}
MAX_CANDIDATES = 150
MAX_MATERIAL = 50_000          # 給模型的候選材料字數上限（context 131k tokens 放得下）
# 問「一類東西」時，程式自己補上這一類常見的說法（光靠模型給的幾個詞，只寫店名或餐點的紀錄會漏掉）
CATEGORY_TERMS = [
    (("吃", "餐", "食", "飯", "麵", "菜單", "美食", "飲料", "喝", "咖啡", "便當", "外送", "外帶", "宵夜", "小吃"),
     ["吃", "餐", "飯", "麵", "便當", "菜單", "餐廳", "小吃", "火鍋", "咖啡", "飲料", "早餐", "午餐", "晚餐", "宵夜",
      "外送", "外帶", "美食", "鍋", "燒肉", "拉麵", "丼", "漢堡", "披薩", "壽司", "牛排", "甜點", "手搖", "茶", "雞", "豬", "牛"]),
]
_TIME_HINT = re.compile(r"\d{1,4}\s*[/\-年月日號週周天]|今天|昨天|前天|明天|上週|上周|本週|這週|這周|上個月|這個月|本月|去年|今年|前年|最近|剛剛|早上|昨晚|今晚|"
                        r"上次|週[一二三四五六日]|星期|禮拜|[一二三四五六七八九十]月|春天|夏天|秋天|冬天|寒假|暑假")


@dataclass
class Query:
    date_from: str = ""
    date_to: str = ""
    terms: list[str] = field(default_factory=list)
    want: str = ""


@dataclass
class Candidate:
    date: str          # YYYY-MM-DD HH:MM（每日總結只有日期）
    type: str
    text: str          # 給模型看的內容
    excerpt: str       # 給人看的節錄
    url: str
    score: float = 0.0


def parse_query(out: str, fallback: str) -> Query:
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            terms = [str(t).strip() for t in d.get("terms", []) if str(t).strip()][:20]
            f, t = str(d.get("from") or ""), str(d.get("to") or "")
            ok = re.compile(r"^\d{4}-\d{2}-\d{2}$")
            return Query(f if ok.match(f) else "", t if ok.match(t) else "", terms or [fallback],
                         str(d.get("want", "")))
        except (json.JSONDecodeError, AttributeError):
            pass
    return Query(terms=[fallback])


def has_time_hint(question: str) -> bool:
    return bool(_TIME_HINT.search(question or ""))


def expand_terms(question: str, terms: list[str]) -> list[str]:
    """問題或搜尋詞屬於某一類（例如吃的）→ 補上這一類的常見詞（不重複，最多 45 個）。"""
    out = list(terms)
    blob = question + " " + " ".join(terms)
    for triggers, extra in CATEGORY_TERMS:
        if any(t in blob for t in triggers):
            out += [t for t in extra if t not in out]
    return out[:45]


def _excerpt(rec, terms: list[str]) -> str:
    """節錄規則：圖片／連結用摘要；檔案用檔名＋摘要首句；文字取命中前後各 10 字或前 20 字。"""
    gist = (rec.gist or "").split("。")[0]
    o = useful(getattr(rec, "ocr", None))
    for t in terms:                          # 命中圖中文字：直接秀那一段
        i = o.find(t) if (o and t) else -1
        if i >= 0:
            s, e = max(0, i - 10), min(len(o), i + len(t) + 10)
            return "🔤" + ("…" if s else "") + o[s:e].replace("\n", " ") + ("…" if e < len(o) else "")
    if rec.type in ("image", "album", "link", "video") and gist:
        return gist
    if rec.type == "file":
        return f"{rec.filename or ''}{('｜' + gist) if gist else ''}"
    text = rec.excerpt or gist or ""
    for t in terms:
        i = text.find(t)
        if i >= 0:
            s, e = max(0, i - 10), min(len(text), i + len(t) + 10)
            return ("…" if s else "") + text[s:e] + ("…" if e < len(text) else "")
    return text[:20] + ("…" if len(text) > 20 else "")


def ocr_window(o: str, terms: list[str], n: int = 600) -> str:
    """圖中文字很長時，取第一個命中詞附近的 n 字（沒命中就取開頭）。"""
    if len(o) <= n:
        return o
    low = o.lower()
    hits = [low.find(t.lower()) for t in terms if t and t.lower() in low]
    if not hits:
        return o[:n] + "…"
    start = max(0, min(hits) - n // 3)
    return ("…" if start else "") + o[start:start + n] + ("…" if start + n < len(o) else "")


def score_text(text: str, terms: list[str]) -> float:
    low = text.lower()
    return sum(1.0 + 0.2 * (low.count(t.lower()) - 1) for t in terms if t and t.lower() in low)


def in_range(day: str, q: Query) -> bool:
    return (not q.date_from or day >= q.date_from) and (not q.date_to or day <= q.date_to)


def select(cands: list[Candidate], q: Query) -> list[Candidate]:
    """候選不多（≤120）就全部交給模型判斷（同義、隱含的說法也找得到）；太多才用搜尋詞分數篩。"""
    cands = [c for c in cands if in_range(c.date[:10], q)]
    if len(cands) <= MAX_CANDIDATES:
        pool = list(cands)
    else:
        pool = [c for c in cands if c.score > 0]
        pool.sort(key=lambda c: (-c.score, c.date))
        pool = pool[:MAX_CANDIDATES]
    pool.sort(key=lambda c: c.date)        # 給模型時依時間排序，比較好理解
    return pool


def collect(index_entries, recaps: dict[str, tuple[str, str]], q: Query) -> list[Candidate]:
    """正式資料：#關鍵字庫 紀錄＋#總結 每日總結。recaps：日期 → (內文, 連結)。"""
    cands: list[Candidate] = []
    for e in index_entries:
        r = e.rec
        if not r.date:
            continue
        o = useful(r.ocr)
        base = [r.excerpt, r.filename and f"檔案：{r.filename}", r.gist and f"重點：{r.gist}",
                r.kw and f"關鍵字：{r.kw}"]
        # 比對用完整的圖中文字（才找得到後段的字）；給模型看的只取命中附近 600 字
        full = "｜".join(x for x in base + [o and f"圖中文字：{o}"] if x)
        text = "｜".join(x for x in base + [o and f"圖中文字：{ocr_window(o, q.terms)}"] if x)
        cands.append(Candidate(r.date, r.type, text or "（無文字）", _excerpt(r, q.terms), r.mirror,
                               score_text(full, q.terms)))
    for day, (body, url) in recaps.items():
        cands.append(Candidate(day, "daily", body[:600], "每日總結", url, score_text(body, q.terms) * 0.8))
    return select(cands, q)


def parse_answer(out: str, n: int) -> tuple[str, list[int]]:
    lines = out.strip().split("\n")
    picks: list[int] = []
    body = []
    for ln in lines:
        m = re.match(r"^\s*相關[:：]\s*(.*)$", ln)
        if m:
            picks = [int(x) for x in re.findall(r"\d+", m.group(1)) if 1 <= int(x) <= n]
        else:
            body.append(ln)
    seen, uniq = set(), []
    for p in picks:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return "\n".join(body).strip(), uniq[:8]


def format_reply(answer: str, cands: list[Candidate], picks: list[int]) -> str:
    urls = [c.url for c in cands]
    lines = [daily.link_citations(answer, urls)] if answer else []
    for i in picks:
        c = cands[i - 1]
        lines.append("")
        lines.append(f"📅 {c.date}｜{TYPE_ICON.get(c.type, c.type)}")
        if c.excerpt:
            lines.append(c.excerpt)
        lines.append(f"[↗ 跳到{'總結' if c.type == 'daily' else '鏡像'}](<{c.url}>)")
    return "\n".join(lines).strip()


async def search(llm, question: str, source, tz_now: datetime,
                 thinking: bool = False) -> tuple[str, Query, int]:
    """source(q) → 候選清單（async）。回傳 (回覆文字, 解析結果, 候選數)。"""
    today = tz_now.strftime("%Y-%m-%d")
    weekday = "星期" + WEEKDAYS[tz_now.weekday()]
    out = await llm.chat(prompts.render("SEARCH_PARSE", today=today, weekday=weekday), question,
                         max_tokens=300)
    q = parse_query(out, question)
    if not has_time_hint(question):                 # 使用者沒提時間：模型自己編的日期範圍一律不用（會把更早的紀錄擋掉）
        q.date_from = q.date_to = ""
    q.terms = expand_terms(question, q.terms)
    cands = await source(q)
    if not cands:
        rng = f"（{q.date_from}～{q.date_to}）" if q.date_from or q.date_to else ""
        return f"找不到相關的訊息 🤔{rng}", q, 0
    items = [daily.Item(c.date[5:], TYPE_ICON.get(c.type, c.type), c.text, c.url) for c in cands]
    material = daily.build_material(items, per_item=max(250, min(2000, MAX_MATERIAL // len(items))),
                                    max_chars=MAX_MATERIAL)
    prompt = prompts.render("SEARCH_ANSWER", question=question)
    ans = await llm.chat(prompt, material, max_tokens=8000 if thinking else 1500, thinking=thinking)
    if not ans.strip() and thinking:                # 思考到被截斷、沒有答案：關掉思考再問一次
        ans = await llm.chat(prompt, material, max_tokens=1500, thinking=False)
    if not ans.strip():
        return "模型這次沒有完整回答（可能是思考太久被截斷），請再問一次 🙏", q, len(cands)
    answer, picks = parse_answer(ans, len(cands))
    reply = format_reply(answer, cands, picks)
    return reply or "找不到相關的訊息 🤔", q, len(cands)
