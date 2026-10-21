# relay-core/tests/test_audio.py
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as N

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from enrich import audio  # noqa: E402


class FakeLLM:
    def __init__(self, model="whisper-large-v3", text="大家好今天開會討論預算，決議下週五前交報告。", song=False):
        self.model, self.text, self.song = model, text, song
        self.calls = []

    async def stt_model(self, preferred=None):
        return self.model

    async def transcribe(self, name, data, model):
        self.calls.append(("stt", name, model))
        return self.text

    async def chat(self, prompt, data=None, **k):
        self.calls.append(("chat", prompt[:6]))
        if "歌曲" in prompt and "說話" in prompt:
            return "歌曲" if self.song else "說話"
        return "- 會議討論預算\n- 下週五前交報告"


class AudioTest(unittest.TestCase):
    def run_it(self, llm, known=None):
        return asyncio.run(audio.analyze_audio(llm, N(audio_stt_model=None), "meet.m4a", b"x", known))

    def test_is_audio(self):
        self.assertTrue(audio.is_audio("a.MP3"))
        self.assertTrue(audio.is_audio("voice_1.ogg"))
        self.assertTrue(audio.is_audio("x.bin", "audio/mpeg"))
        self.assertFalse(audio.is_audio("a.png", "image/png"))
        self.assertFalse(audio.is_audio("v.mp4", "video/mp4"))

    def test_talk_gets_summary_and_transcript(self):
        r = self.run_it(FakeLLM())
        self.assertEqual(r.kind, "talk")
        self.assertIn("預算", r.summary)
        self.assertIn("開會", r.transcript)

    def test_song_is_not_summarised(self):
        r = self.run_it(FakeLLM(song=True))
        self.assertEqual(r.kind, "song")
        self.assertIsNone(r.transcript)
        self.assertIn("歌曲", r.summary)

    def test_no_model_and_empty(self):
        self.assertEqual(self.run_it(FakeLLM(model=None)).kind, "nomodel")
        self.assertEqual(self.run_it(FakeLLM(text="  ")).kind, "empty")

    def test_known_transcript_skips_stt(self):
        llm = FakeLLM(model=None)                            # 沒有語音模型也能用舊逐字稿重新分析
        r = self.run_it(llm, known="這是之前轉好的逐字稿，內容足夠長。")
        self.assertEqual(r.kind, "talk")
        self.assertFalse([c for c in llm.calls if c[0] == "stt"])

    def test_long_transcript_map_reduce(self):
        llm = FakeLLM(text=("今天討論預算。" * 3000))
        r = self.run_it(llm)
        chats = [c for c in llm.calls if c[0] == "chat"]
        self.assertGreater(len(chats), 3)                    # 歌曲判斷＋多段＋合併
        self.assertEqual(r.kind, "talk")

    def test_split_text_bounds(self):
        parts = audio.split_text("句子。" * 5000, 1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual("".join(parts), "句子。" * 5000)


if __name__ == "__main__":
    unittest.main()
