# relay-core/enrich/link.py
"""連結摘要（第 7.3 節）：trafilatura 抽正文 → LLM 2～3 句；失敗退 og 標籤；再失敗標記無法讀取。"""
from __future__ import annotations

import asyncio
import html
import re

import httpx

import prompts
from llm import LLM

# 只收 ASCII 網址字元，遇到中文或全形標點就斷開
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&*+,;=%\[\]]+")
MAX_BYTES = 3 * 1024 * 1024
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) InboxRelay/0.3"


def find_urls(text: str) -> list[str]:
    seen, out = set(), []
    for u in URL_RE.findall(text or ""):
        u = u.rstrip(".,;:!?。，；：！？")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _og(page: str) -> str:
    def meta(prop: str) -> str:
        m = re.search(rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']*)', page, re.I) \
            or re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{prop}["\']', page, re.I)
        return html.unescape(m.group(1)).strip() if m else ""
    title = meta("og:title") or (re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S) or [None, ""])[1]
    desc = meta("og:description") or meta("description")
    return "\n".join(x for x in (html.unescape(title or "").strip(), desc) if x)


def _extract(page: str) -> str:
    import trafilatura  # 延遲載入，啟動較快
    return trafilatura.extract(page, include_comments=False, include_tables=True) or ""


def human_size(n: int | None) -> str:
    if not n:
        return "大小不明"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return ""


def file_link_note(url: str, ctype: str, length: int | None, final_url: str = "") -> str:
    """下載連結（不是網頁）：只讀回應標頭，不下載內容。"""
    from urllib.parse import unquote, urlparse
    name = unquote(urlparse(final_url or url).path.rsplit("/", 1)[-1]) or "（無檔名）"
    kind = (ctype.split(";")[0].strip() or "未知類型")
    return f"- 這是檔案下載連結，不是網頁（未下載）\n- 檔名：{name}\n- 類型：{kind}｜大小：{human_size(length)}"


async def fetch_text(url: str) -> tuple[str, str]:
    """回傳 (文字, 來源類型 article|og|file|"")。只抓 HTML，不跑 JS。

    不是 HTML（例如 .ipsw、.zip、影片）時只看回應標頭就關掉連線，不會把檔案下載下來；
    回傳的「文字」是檔名、類型與大小的說明。"""
    from enrich import screenshot
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": UA},
                                 verify=not screenshot.IGNORE_CERT) as c:
        async with c.stream("GET", url) as r:
            ctype = r.headers.get("content-type", "")
            if r.status_code >= 400:
                return "", ""
            if "html" not in ctype and "xml" not in ctype:
                length = r.headers.get("content-length")
                return file_link_note(url, ctype, int(length) if length and length.isdigit() else None,
                                      str(r.url)), "file"
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    break
            page = buf.decode(r.encoding or "utf-8", errors="ignore")
    text = await asyncio.to_thread(_extract, page)
    if len(text) >= 80:
        return text, "article"
    og = _og(page)
    return (og, "og") if og else ("", "")


async def summarize_link(llm: LLM, url: str) -> str:
    from enrich import screenshot
    if not screenshot.ALLOW_PRIVATE and not await screenshot.is_public_url(url):
        return "（本機或區網網址，PRIVATE_URLS=false，不讀取）"
    try:
        text, kind = await fetch_text(url)
    except Exception as e:  # 網路錯誤、逾時
        return f"無法讀取（{type(e).__name__}）"
    if not text:
        return "無法讀取"
    if kind == "file":
        return text            # 下載連結：直接列出檔名、類型、大小，不需要 AI
    note = "（僅根據網頁標題與描述）" if kind == "og" else ""
    s = await llm.chat(prompts.get("LINK"), f"網址：{url}\n\n{text[:12000]}", max_tokens=300)
    return s + note
