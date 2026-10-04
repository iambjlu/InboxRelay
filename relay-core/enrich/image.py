# relay-core/enrich/image.py
"""圖片摘要。模型不支援圖片（LOCAL_AI_IMAGE_SUPPORT=false）時直接略過。"""
from __future__ import annotations

import prompts
from llm import LLM

MIME = {"png": "image/png", "gif": "image/gif", "webp": "image/webp", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


SUPPORTED = ("image/jpeg", "image/png", "image/webp", "image/gif")
MAX_SIDE = 2048


def detect_mime(data: bytes) -> str | None:
    """看檔頭判斷真正的格式（不信副檔名）；認不得回傳 None。"""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return None


def prepare_image(data: bytes, filename: str = "") -> tuple[bytes, str] | None:
    """給 LLM 的圖：JPEG／PNG／WebP／GIF 原樣用；HEIC、TIFF、BMP… 在記憶體裡轉成 JPEG（最長邊 ≤ 2048）。轉不了回傳 None。"""
    mime = detect_mime(data)
    if mime in SUPPORTED:
        return data, mime
    try:
        import io
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=88)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return None


def mime_of(filename: str) -> str:
    return MIME.get(filename.rsplit(".", 1)[-1].lower(), "image/jpeg")


async def summarize_image(llm: LLM, data: bytes, filename: str, caption: str = "") -> str:
    if not llm.image_support:
        return "（模型未開啟圖片支援）"
    import asyncio
    import openai
    prep = await asyncio.to_thread(prepare_image, data, filename)
    if prep is None:
        return "（圖片格式無法轉換，略過）"
    data, mime = prep
    prompt = prompts.get("IMAGE")
    try:
        return await llm.chat(prompt, data=f"使用者附註：{caption}" if caption else None,
                              image=data, image_mime=mime, max_tokens=400)
    except openai.BadRequestError as e:            # 壞掉／格式不支援的圖：不要讓整則訊息的分析跟著失敗
        return f"（圖片無法解析，略過：{str(getattr(e, 'message', e))[:60]}）"
