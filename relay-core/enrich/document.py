# relay-core/enrich/document.py
"""檔案總結（第 7.4 節）：只讀不執行、只在記憶體處理。

安全規則：
- 不執行任何內容：不呼叫子行程、不 eval/exec、不 import 使用者檔案、不渲染 HTML、不跑巨集。
- 全程 BytesIO，不寫入硬碟。
- Office 檔（zip）先檢查解壓後總大小 ≤ 50MB、檔案數 ≤ 2000，防解壓炸彈。
- XML 一律用 defusedxml。
"""
from __future__ import annotations

import io
import zipfile

import prompts
from llm import LLM

TEXT_EXT = {"txt", "md", "csv", "tsv", "log", "json", "yaml", "yml", "ini", "toml", "cfg", "conf"}
CODE_EXT = {"py", "js", "ts", "tsx", "jsx", "swift", "java", "kt", "c", "h", "cpp", "hpp", "cs", "go",
            "rs", "rb", "php", "sh", "bash", "zsh", "ps1", "bat", "cmd", "sql", "r", "m", "lua", "pl"}
WEB_EXT = {"html", "htm", "xml"}
OFFICE_EXT = {"docx", "xlsx", "pptx", "docm", "xlsm", "pptm"}
PDF_EXT = {"pdf"}
MAX_PDF_PAGES = 60          # 文字版 PDF 最多讀幾頁
OCR_PAGES = 3               # 掃描版 PDF：把前幾頁轉成圖片交給 Qwen 看
LEGACY_EXT = {"doc", "xls", "ppt"}

MAX_UNZIPPED = 50 * 1024 * 1024
MAX_MEMBERS = 2000
MAX_CHARS = 30_000
CHUNK = 10_000


class Unsupported(Exception):
    pass


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_supported(name: str) -> bool:
    e = ext_of(name)
    return e in TEXT_EXT | CODE_EXT | WEB_EXT | OFFICE_EXT | PDF_EXT


def _decode(data: bytes) -> str:
    from charset_normalizer import from_bytes
    best = from_bytes(data).best()
    return str(best) if best is not None else data.decode("utf-8", errors="ignore")


def _check_zip(data: bytes) -> zipfile.ZipFile:
    zf = zipfile.ZipFile(io.BytesIO(data))
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise Unsupported(f"壓縮內容檔案數過多（{len(infos)}）")
    if sum(i.file_size for i in infos) > MAX_UNZIPPED:
        raise Unsupported("解壓後超過 50MB，疑似解壓炸彈，放棄")
    return zf


def _xml_texts(xml: bytes, tag_suffix: str) -> list[str]:
    from defusedxml import ElementTree as ET
    root = ET.fromstring(xml)
    return [el.text for el in root.iter() if el.tag.endswith(tag_suffix) and el.text]


def _docx_raw(zf: zipfile.ZipFile) -> str:
    # 也適用 docm：只讀 document.xml 的文字節點，巨集（vbaProject.bin）完全不碰
    return "".join(_xml_texts(zf.read("word/document.xml"), "}t"))


def _pptx_raw(zf: zipfile.ZipFile) -> str:
    slides = sorted((n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")),
                    key=lambda n: int("".join(c for c in n if c.isdigit()) or 0))
    out = []
    for i, n in enumerate(slides, 1):
        out.append(f"[第 {i} 張投影片] " + " ".join(_xml_texts(zf.read(n), "}t")))
    return "\n".join(out)


def _xlsx(data: bytes) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_vba=False)
    out = []
    try:
        for ws in wb.worksheets:
            out.append(f"[工作表 {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                if any(cells):
                    out.append("\t".join(cells))
                if sum(len(x) for x in out) > MAX_CHARS * 2:
                    break
    finally:
        wb.close()
    return "\n".join(out)


def _docx_lib(data: bytes) -> str:
    import docx
    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for r in t.rows:
            parts.append("\t".join(c.text for c in r.cells))
    return "\n".join(parts)


def _pptx_lib(data: bytes) -> str:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    out = []
    for i, slide in enumerate(prs.slides, 1):
        texts = [sh.text_frame.text for sh in slide.shapes if getattr(sh, "has_text_frame", False)]
        out.append(f"[第 {i} 張投影片] " + " ".join(t for t in texts if t.strip()))
    return "\n".join(out)


def _pdf_text(data: bytes) -> str:
    """只讀文字層：不執行 PDF 裡的 JavaScript、不開啟內嵌檔案或連結。"""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise Unsupported("PDF 有密碼保護，無法讀取")
        except Exception:
            raise Unsupported("PDF 有密碼保護，無法讀取")
    out = []
    for i, page in enumerate(reader.pages[:MAX_PDF_PAGES], 1):
        t = (page.extract_text() or "").strip()
        if t:
            out.append(f"[第 {i} 頁] {t}")
        if sum(len(x) for x in out) > MAX_CHARS:
            break
    if len(reader.pages) > MAX_PDF_PAGES:
        out.append(f"（共 {len(reader.pages)} 頁，只讀前 {MAX_PDF_PAGES} 頁）")
    return "\n".join(out)


def pdf_page_images(data: bytes, pages: int = OCR_PAGES, scale: float = 1.6) -> list[bytes]:
    """把前幾頁轉成 PNG（全程在記憶體），給掃描版 PDF 用。"""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(data)
    imgs: list[bytes] = []
    try:
        for i in range(min(pages, len(pdf))):
            pil = pdf[i].render(scale=scale).to_pil()
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            imgs.append(buf.getvalue())
    finally:
        pdf.close()
    return imgs


def extract_text(name: str, data: bytes) -> str:
    """把檔案內容讀成純文字（同步；請用 asyncio.to_thread 呼叫）。"""
    e = ext_of(name)
    if e in LEGACY_EXT:
        raise Unsupported("舊版二進位 Office 格式不支援，只記錄檔名")
    if e in TEXT_EXT or e in CODE_EXT:
        return _decode(data)  # 程式碼與腳本一律當純文字
    if e in WEB_EXT:
        text = _decode(data)
        if e in ("html", "htm"):
            import trafilatura
            return trafilatura.extract(text) or ""
        from defusedxml import ElementTree as ET
        root = ET.fromstring(data)
        return " ".join(t.strip() for t in root.itertext() if t.strip())
    if e in PDF_EXT:
        return _pdf_text(data)
    if e in OFFICE_EXT:
        zf = _check_zip(data)
        if e == "docx":
            return _docx_lib(data)
        if e == "docm":
            return _docx_raw(zf)
        if e in ("xlsx", "xlsm"):
            return _xlsx(data)
        if e == "pptx":
            return _pptx_lib(data)
        if e == "pptm":
            return _pptx_raw(zf)
    raise Unsupported("不支援的檔案類型")


async def summarize_document(llm: LLM, name: str, data: bytes) -> str:
    import asyncio
    try:
        text = await asyncio.to_thread(extract_text, name, data)
    except Unsupported as e:
        return f"（{e}）"
    except Exception as e:
        return f"（無法讀取：{type(e).__name__}）"
    text = text.strip()
    if ext_of(name) in PDF_EXT and len(text) < 80:
        # 幾乎沒有文字層 → 多半是掃描檔，改成把前幾頁轉圖片交給 Qwen 看
        return await _summarize_scanned_pdf(llm, name, data)
    del data
    if not text:
        return "（檔案沒有可讀的文字）"
    truncated = len(text) > MAX_CHARS
    text = text[:MAX_CHARS]
    ask = prompts.render("DOC", name=name)
    if len(text) <= CHUNK + 2000:
        s = await llm.chat(ask, text, max_tokens=500)
    else:
        parts = []
        for i in range(0, len(text), CHUNK):
            parts.append(await llm.chat(prompts.render("DOC_PART", name=name, i=i // CHUNK + 1),
                                        text[i:i + CHUNK], max_tokens=300))
        s = await llm.chat(ask + prompts.get("DOC_MERGE"), "\n".join(parts), max_tokens=500)
    return s + ("（檔案很長，只讀了前 30,000 字）" if truncated else "")


async def _summarize_scanned_pdf(llm: LLM, name: str, data: bytes) -> str:
    import asyncio
    if not llm.image_support:
        return "（這是掃描版 PDF，沒有文字層；模型未開啟圖片支援，無法閱讀）"
    try:
        imgs = await asyncio.to_thread(pdf_page_images, data)
    except Exception as e:
        return f"（掃描版 PDF 轉圖片失敗：{type(e).__name__}）"
    finally:
        del data
    if not imgs:
        return "（PDF 沒有頁面）"
    parts = []
    for i, img in enumerate(imgs, 1):
        parts.append(await llm.chat(prompts.render("DOC_PART", name=name, i=i) + "（這一段是 PDF 第 "
                                    f"{i} 頁的圖片，請讀出上面的文字重點）", image=img, image_mime="image/png",
                                    max_tokens=400))
    imgs.clear()
    if len(parts) == 1:
        return parts[0] + "\n（掃描版 PDF，依頁面圖片判讀）"
    s = await llm.chat(prompts.render("DOC", name=name) + prompts.get("DOC_MERGE"), "\n".join(parts), max_tokens=500)
    return s + f"\n（掃描版 PDF，依前 {len(parts)} 頁圖片判讀）"
