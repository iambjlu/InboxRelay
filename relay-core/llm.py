# relay-core/llm.py
"""oMLX（OpenAI 相容 API）呼叫。模型一次只處理一件事（單線排隊，第 9.2 節）。"""
from __future__ import annotations

import asyncio
import base64
import logging
import re

from openai import AsyncOpenAI

import prompts
from mdtable import tables_to_bullets

log = logging.getLogger("llm")

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def is_offline_error(e: BaseException) -> bool:
    """外部 LLM 連不上／伺服器掛了／模型沒載入：這種錯誤不是內容問題，工作要留著下次再做。"""
    import openai
    import httpx
    return isinstance(e, (openai.APIConnectionError, openai.InternalServerError, openai.NotFoundError,
                          httpx.ConnectError, httpx.TimeoutException, ConnectionError))

async def test_connection(base_url: str, model: str, api_key: str) -> dict:
    """設定頁「測試連線」：用表單目前的值（不動執行中的 LLM）只呼叫 GET /models（回 200＝端點與 Key 正常），不會叫模型產生內容。
    回傳 {"ok": bool, "message": str, "ms": int}。"""
    import time
    import openai
    if not base_url.strip():
        return {"ok": False, "message": "尚未填寫端點", "ms": 0}
    client = AsyncOpenAI(base_url=base_url.strip().rstrip("/"), api_key=api_key or "none", timeout=20, max_retries=0)
    t0 = time.monotonic()

    def ms() -> int:
        return int((time.monotonic() - t0) * 1000)
    try:
        models = [m.id for m in (await client.models.list()).data]
        if model and models and model not in models:
            return {"ok": False, "ms": ms(),
                    "message": f"已連線，但找不到模型「{model}」（可用：{', '.join(models[:5])}）"}
        shown = f"模型「{model}」可用" if model and models else f"共 {len(models)} 個模型"
        return {"ok": True, "ms": ms(), "message": f"連線成功（{ms()} ms）：{shown}"}
    except openai.AuthenticationError:
        return {"ok": False, "ms": ms(), "message": "API Key 不正確（401/403）"}
    except openai.APIConnectionError as e:
        return {"ok": False, "ms": ms(), "message": f"連不上 {base_url}：{type(e.__cause__ or e).__name__}"}
    except openai.APIStatusError as e:
        return {"ok": False, "ms": ms(), "message": f"伺服器回應 {e.status_code}：{str(e.message)[:120]}"}
    except Exception as e:
        return {"ok": False, "ms": ms(), "message": f"{type(e).__name__}: {str(e)[:120]}"}
    finally:
        await client.close()


class LLM:
    def __init__(self, base_url: str, model: str, api_key: str, image_support: bool = True) -> None:
        self.model = model
        self.image_support = image_support
        self.client = AsyncOpenAI(base_url=base_url.rstrip("/"), api_key=api_key or "none",
                                  timeout=300, max_retries=1)
        self._lock = asyncio.Semaphore(1)
        self.memory: str = ""          # 最新 #記憶 的內容，由 memory.MemoryStore 設定
        self.memory_version: str = ""
        self._avg_tokens: dict[int, float] = {}   # 每種 max_tokens 實際平均產生多少 token（估進度用）
        self._interactive_waiting = 0             # 有 AI 對話在等 → 背景工作先讓

    def _system(self, use_memory: bool) -> str:
        base = prompts.get("GUARD") + "\n" + prompts.FORMAT_RULE
        if use_memory and self.memory.strip():
            return base + prompts.render("MEMORY_WRAP", memory=self.memory.strip())
        return base

    async def ping(self) -> list[str]:
        models = await self.client.models.list()
        return [m.id for m in models.data]

    async def chat(self, prompt: str, data: str | None = None, image: bytes | None = None,
                   image_mime: str = "image/jpeg", max_tokens: int = 800, thinking: bool = False,
                   use_memory: bool = True) -> str:
        user_text = prompt if data is None else f"{prompt}\n\n<data>\n{data}\n</data>"
        if image is not None:
            b64 = base64.b64encode(image).decode()
            content = [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{b64}"}},
            ]
        else:
            content = user_text
        import activity
        task = activity.bound()
        kwargs = dict(
            model=self.model,
            messages=[{"role": "system", "content": self._system(use_memory)},
                      {"role": "user", "content": content}],
            max_tokens=max_tokens,
            temperature=0.3,
            # 短摘要與關鍵字關閉 thinking，速度快很多（第 3.4 節）
            extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
        )
        if task is not None:                       # 背景工作：有人在對話就先讓
            while self._interactive_waiting:
                await asyncio.sleep(0.3)
        async with self._lock:
            if task is None:
                resp = await self.client.chat.completions.create(**kwargs)
                text = resp.choices[0].message.content or ""
            else:
                text = await self._stream(kwargs, task, max_tokens)
        return tables_to_bullets(_THINK_RE.sub("", text).strip())

    async def chat_messages(self, messages: list[dict], max_tokens: int = 4000, thinking: bool = False,
                            temperature: float = 0.6, progress=None) -> tuple[str, dict]:
        """多輪對話（AI 對話頻道用）：messages 是完整的 OpenAI 格式（含 system）。優先於背景工作。
        回傳 (回覆, 用量 {prompt_tokens, completion_tokens, total_tokens})；端點沒給用量時是空的。"""
        self._interactive_waiting += 1
        acquired = False
        try:
            async with self._lock:
                self._interactive_waiting -= 1
                acquired = True
                if progress is not None:             # 串流：邊收邊回報已產生的 token 數（壓縮進度條用）
                    parts: list[str] = []
                    n = 0
                    stream = await self.client.chat.completions.create(
                        model=self.model, messages=messages, max_tokens=max_tokens, temperature=temperature,
                        stream=True, extra_body={"chat_template_kwargs": {"enable_thinking": thinking}})
                    async for chunk in stream:
                        if chunk.choices and chunk.choices[0].delta.content:
                            parts.append(chunk.choices[0].delta.content)
                            n += 1
                            progress(n)
                    text = _THINK_RE.sub("", "".join(parts))
                    if "</think>" in text:
                        text = text.split("</think>", 1)[1]
                    return tables_to_bullets(text.strip()), {}
                resp = await self.client.chat.completions.create(
                    model=self.model, messages=messages, max_tokens=max_tokens, temperature=temperature,
                    extra_body={"chat_template_kwargs": {"enable_thinking": thinking}})
        finally:
            if not acquired:                       # 等鎖時被取消／出錯：把等待數還回去
                self._interactive_waiting -= 1
        text = resp.choices[0].message.content or ""
        text = _THINK_RE.sub("", text)
        if "</think>" in text:                     # 沒有開頭標籤的思考內容
            text = text.split("</think>", 1)[1]
        usage = {}
        u = getattr(resp, "usage", None)
        if u is not None:
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                v = getattr(u, k, None)
                if isinstance(v, int):
                    usage[k] = v
        return tables_to_bullets(text.strip()), usage

    async def _stream(self, kwargs: dict, task, max_tokens: int) -> str:
        """串流接收，邊收邊回報進度（已產生的 token ÷ 這類呼叫平均長度）。"""
        expect = self._avg_tokens.get(max_tokens, max_tokens * 0.4)
        parts: list[str] = []
        n = 0
        stream = await self.client.chat.completions.create(stream=True, **kwargs)
        async for chunk in stream:
            if not chunk.choices:
                continue
            piece = chunk.choices[0].delta.content or ""
            if piece:
                parts.append(piece)
                n += 1
                if n % 4 == 0:
                    task.set_llm(n / max(expect, 1))
        self._avg_tokens[max_tokens] = expect * 0.7 + n * 0.3 if n else expect
        task.set_llm(0.95)
        return "".join(parts)
