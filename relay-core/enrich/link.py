# relay-core/enrich/link.py
"""連結摘要（第 7.3 節）：trafilatura 抽正文 → LLM 2～3 句；失敗退 og 標籤；再失敗標記無法讀取。"""
from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass

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


IMAGE_MAX = 20 * 1024 * 1024      # 連到圖片的網址：最多下載這麼大來分析


def sniff_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return "image/jpeg"


async def fetch_link(url: str, want_image: bool = False) -> tuple[str, str, bytes | None]:
    """回傳 (文字, 種類 article|og|file|image|"", 圖片 bytes)。只抓 HTML，不跑 JS。

    不是 HTML（例如 .ipsw、.zip、影片）時只看回應標頭就關掉連線，不會把檔案下載下來；
    回傳的「文字」是檔名、類型與大小的說明。want_image＝連到圖片（≤20MB）時把圖下載下來。"""
    from enrich import screenshot
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": UA},
                                 verify=not screenshot.IGNORE_CERT) as c:
        async with c.stream("GET", url) as r:
            ctype = r.headers.get("content-type", "")
            if r.status_code >= 400:
                return "", "", None
            length = r.headers.get("content-length")
            size = int(length) if length and length.isdigit() else None
            if want_image and ctype.startswith("image/") and "svg" not in ctype and (size or 0) <= IMAGE_MAX:
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > IMAGE_MAX:
                        break
                if 0 < len(buf) <= IMAGE_MAX:
                    return file_link_note(url, ctype, len(buf), str(r.url)), "image", bytes(buf)
            if "html" not in ctype and "xml" not in ctype:
                return file_link_note(url, ctype, size, str(r.url)), "file", None
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    break
            page = buf.decode(r.encoding or "utf-8", errors="ignore")
    text = await asyncio.to_thread(_extract, page)
    og = _og(page)
    if len(text) >= 80:
        # 正文可能混到導覽列、廣告：連同網頁標籤（標題、描述）一起給，兩邊互相佐證
        return (f"網頁標籤：\n{og}\n\n正文：\n{text}" if og else text), "article", None
    return (og, "og", None) if og else ("", "", None)


async def fetch_text(url: str) -> tuple[str, str]:
    text, kind, _ = await fetch_link(url)
    return text, kind


@dataclass
class LinkResult:
    title: str                  # 回覆裡的標題（🔗 網址／🖼️ 網址）
    summary: str
    image: bytes | None = None  # 連到圖片時的圖
    failed: bool = False        # 什麼都讀不到


async def probe_link(url: str) -> tuple[str, str, bytes | None, str]:
    """先抓一次（之後摘要直接用）：回傳 (文字, 種類, 圖片, 錯誤說明)。"""
    from enrich import screenshot
    if not screenshot.ALLOW_PRIVATE and not await screenshot.is_public_url(url):
        return "", "private", None, ""
    try:
        text, kind, data = await fetch_link(url, want_image=True)
        return text, kind, data, ""
    except Exception as e:  # 網路錯誤、逾時
        return "", "", None, type(e).__name__


RENDERABLE = ("text/", "image/", "application/pdf", "application/json", "application/xml", "application/xhtml")
BINARY_MAX = 100 * 1024 * 1024


async def probe_kind(url: str) -> str:
    """只看回應標頭判斷這個網址能不能截圖（截圖車道用，不下載內文）：
    private（內網被擋）／binary（zip、影片、安裝檔等瀏覽器不能預覽的檔案，或大到 100MB 以上）／""（網頁、PDF、文字檔、圖片…，或讀不到，交給截圖）。"""
    from enrich import screenshot
    if not screenshot.ALLOW_PRIVATE and not await screenshot.is_public_url(url):
        return "private"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10, headers={"User-Agent": UA},
                                     verify=not screenshot.IGNORE_CERT) as c:
            async with c.stream("GET", url) as r:
                ctype = r.headers.get("content-type", "").lower()
                if r.status_code >= 400:
                    return ""
                length = r.headers.get("content-length")
                if length and length.isdigit() and int(length) > BINARY_MAX:
                    return "binary"
                if "html" in ctype or "xml" in ctype or ctype.startswith(RENDERABLE):
                    return ""
                if ctype:
                    return "binary"
    except Exception:
        pass
    return ""


def embed_preview(message, url: str) -> str:
    """Discord 幫鏡像訊息產生的網頁預覽（標題、描述、網站名稱）。"""
    embeds = list(getattr(message, "embeds", None) or [])
    if not embeds:
        return ""
    def norm(u: str) -> str:
        return (u or "").rstrip("/").split("#", 1)[0]
    match = [e for e in embeds if norm(getattr(e, "url", "") or "") == norm(url)]
    if not match and len(embeds) == 1 and len(find_urls(getattr(message, "content", "") or "")) == 1:
        match = embeds
    out = []
    for e in match[:1]:
        prov = getattr(getattr(e, "provider", None), "name", None)
        auth = getattr(getattr(e, "author", None), "name", None)
        for label, v in (("網站", prov), ("作者", auth), ("標題", e.title), ("描述", e.description)):
            if v:
                out.append(f"{label}：{v}")
    return "\n".join(out)[:1500]


async def summarize_probed(llm: LLM, url: str, probed: tuple[str, str, bytes | None, str],
                           preview: str = "", shot: bytes | None = None, caption: str = "",
                           describe_image: bool = True) -> LinkResult:
    """依 probe_link 的結果做摘要。網頁讀不到正文時改用「網頁預覽＋截圖」。"""
    text, kind, data, err = probed
    if kind == "private":
        return LinkResult(f"🔗 {url}", "（本機或區網網址，PRIVATE_URLS=false，不讀取）")
    if kind == "image" and data:
        if not describe_image:
            return LinkResult(f"🖼️ {url}", text, image=data)
        if not llm.image_support:
            return LinkResult(f"🖼️ {url}", "（模型未開啟圖片支援）", image=data)
        from enrich.image import prepare_image
        prep = await asyncio.to_thread(prepare_image, data, url)
        if prep is None:
            return LinkResult(f"🖼️ {url}", "（圖片格式無法轉換，略過）", image=data)
        s = await llm.chat(prompts.get("IMAGE"), data=f"使用者附註：{caption}" if caption else None,
                           image=prep[0], image_mime=prep[1], max_tokens=400)
        return LinkResult(f"🖼️ {url}", s, image=data)
    if kind == "file":
        return LinkResult(f"🔗 {url}", text)
    if kind == "article":
        extra = f"\n\n網頁預覽：\n{preview}" if preview and preview not in text else ""
        hint = "（正文可能混有導覽列或廣告文字，請以網頁標籤與整體內容判斷這個網頁真正在講什麼）\n\n" if "網頁標籤：" in text else ""
        s = await llm.chat(prompts.get("LINK"), f"網址：{url}\n\n{hint}{text[:12000]}{extra}", max_tokens=300)
        return LinkResult(f"🔗 {url}", s)
    # 讀不到正文（只有 og 標籤或完全讀不到）→ 用網頁預覽與截圖
    material = "\n".join(x for x in (text and f"網頁標籤：\n{text}", preview and f"網頁預覽：\n{preview}") if x)
    use_shot = shot is not None and llm.image_support
    if material or use_shot:
        s = await llm.chat(prompts.get("LINK_FALLBACK"), f"網址：{url}\n\n{material or '（沒有文字資訊，請看截圖）'}",
                           image=shot if use_shot else None, image_mime=sniff_mime(shot) if use_shot else "image/jpeg",
                           max_tokens=300)
        src = "、".join(x for x, ok in (("網頁標籤", bool(text)), ("網頁預覽", bool(preview)), ("截圖", use_shot)) if ok)
        return LinkResult(f"🔗 {url}", f"{s}（根據{src}）")
    return LinkResult(f"🔗 {url}", f"無法讀取（{err}）" if err else "無法讀取", failed=True)


async def summarize_link(llm: LLM, url: str) -> str:
    return (await summarize_probed(llm, url, await probe_link(url))).summary
