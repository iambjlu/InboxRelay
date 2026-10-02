# relay-core/enrich/image.py
"""圖片摘要。模型不支援圖片（LOCAL_AI_IMAGE_SUPPORT=false）時直接略過。"""
from __future__ import annotations

import prompts
from llm import LLM

MIME = {"png": "image/png", "gif": "image/gif", "webp": "image/webp", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


def mime_of(filename: str) -> str:
    return MIME.get(filename.rsplit(".", 1)[-1].lower(), "image/jpeg")


async def summarize_image(llm: LLM, data: bytes, filename: str, caption: str = "") -> str:
    if not llm.image_support:
        return "（模型未開啟圖片支援）"
    prompt = prompts.get("IMAGE")
    return await llm.chat(prompt, data=f"使用者附註：{caption}" if caption else None,
                          image=data, image_mime=mime_of(filename), max_tokens=400)
