# relay-core/enrich/audio.py
"""音檔理解：語音轉文字（oMLX 的語音模型）→ 判斷是不是歌曲 → 條列摘要；按鈕再產生詳細整理。

- 轉文字交給外部伺服器的語音模型（Whisper 類）；Qwen 本身聽不懂音檔。伺服器沒載入語音模型就回報提示，不影響其他分析。
- 逐字稿放在摘要訊息的附件 `<檔名>.txt`（Discord 保存，按鈕與重新分析都從那裡讀，不用再轉一次）。
- 歌曲（歌詞／副歌）不做摘要。全程在記憶體處理。
"""
from __future__ import annotations

from dataclasses import dataclass

import prompts
from llm import LLM

AUDIO_EXT = {"mp3", "m4a", "wav", "aac", "flac", "ogg", "oga", "opus", "amr", "wma", "caf", "aif", "aiff", "mp2"}
MAX_TRANSCRIPT = 150_000      # 逐字稿最多處理幾字（約 3～4 小時的會議）
CHUNK = 10_000
SINGLE = 12_000               # 不超過這個字數一次摘要完
MIN_CHARS = 8                 # 少於這個字數＝沒有語音內容
NO_MODEL = "（伺服器還沒有語音轉文字模型：請在 oMLX 載入 Whisper 類模型，或在設定填 AUDIO_STT_MODEL）"


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_audio(name: str, content_type: str | None = None) -> bool:
    return (content_type or "").lower().startswith("audio/") or ext_of(name) in AUDIO_EXT


def transcript_name(name: str) -> str:
    return f"{name}.txt"


@dataclass
class AudioResult:
    summary: str                  # 摘要訊息裡「🎙️ 檔名」底下的內容（條列）
    transcript: str | None = None  # 要存成附件的逐字稿（沒有就不附）
    kind: str = "talk"            # talk／song／empty／nomodel


def split_text(text: str, n: int = CHUNK) -> list[str]:
    """依換行／句號盡量在邊界切成 ≤ n 字的幾段。"""
    out, rest = [], text
    while len(rest) > n:
        cut = max(rest.rfind(c, 0, n) for c in ("\n", "。", "！", "？", ". ", " "))
        cut = cut + 1 if cut > n // 2 else n
        out.append(rest[:cut])
        rest = rest[cut:]
    if rest.strip():
        out.append(rest)
    return out


async def is_song(llm: LLM, transcript: str) -> bool:
    sample = transcript[:1500]
    out = await llm.chat(prompts.get("AUDIO_KIND"), sample, max_tokens=10, use_memory=False)
    return out.strip().startswith("歌曲")


async def analyze_audio(llm: LLM, cfg, name: str, data: bytes, known: str | None = None) -> AudioResult:
    """known＝之前已經轉好的逐字稿（重新分析時不再轉一次）。外部 LLM 連不上的錯誤照常往外丟（工作留著下次做）。"""
    import openai
    transcript = known
    if transcript is None:
        model = await llm.stt_model(cfg.audio_stt_model)
        if model is None:
            return AudioResult(NO_MODEL, None, "nomodel")
        try:
            transcript = await llm.transcribe(name, data, model)
        except openai.BadRequestError as e:
            return AudioResult(f"（語音轉文字失敗：{str(e.message)[:100]}）", None, "empty")
    transcript = transcript.strip()
    if len(transcript) < MIN_CHARS:
        return AudioResult("（沒有偵測到語音內容，可能是純音樂或靜音）", None, "empty")
    if await is_song(llm, transcript):
        return AudioResult("- 這看起來是歌曲／音樂，不做摘要", None, "song")
    return AudioResult(await summarize(llm, name, transcript), transcript, "talk")


async def _map_reduce(llm: LLM, name: str, transcript: str, whole: str, part: str, merge: str, tokens: int) -> str:
    text = transcript[:MAX_TRANSCRIPT]
    if len(text) <= SINGLE:
        return await llm.chat(prompts.render(whole, name=name), text, max_tokens=tokens)
    notes = []
    for i, chunk in enumerate(split_text(text), 1):
        notes.append(await llm.chat(prompts.render(part, name=name, i=i), chunk, max_tokens=tokens))
    return await llm.chat(prompts.get(merge), "\n\n".join(notes), max_tokens=tokens)


async def summarize(llm: LLM, name: str, transcript: str) -> str:
    return await _map_reduce(llm, name, transcript, "AUDIO", "AUDIO_PART", "AUDIO_MERGE", 700)


async def detailed_notes(llm: LLM, name: str, transcript: str) -> str:
    """按鈕用：詳細條列重點＋總結。"""
    return await _map_reduce(llm, name, transcript, "AUDIO_NOTES", "AUDIO_NOTES_PART", "AUDIO_NOTES_MERGE", 3000)
