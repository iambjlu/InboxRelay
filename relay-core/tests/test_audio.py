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


class WorkerTest(unittest.TestCase):
    def _setup(self, llm, with_model=True):
        import records
        from enrich.audioworker import AudioWorker
        try:
            from test_m4 import FakeIndex, FakeMsg, rec
        except ImportError:
            from tests.test_m4 import FakeIndex, FakeMsg, rec

        class Att:
            def __init__(self, name):
                self.filename, self.size, self.content_type = name, 1000, "audio/mp4"

            async def read(self):
                return b"audio"
        mirror = FakeMsg("# 2026-10-02\n", [Att("meeting.m4a"), Att("song.mp3")])
        seen = {}
        orig = mirror.reply

        async def reply(text, **kw):
            seen["kw"] = kw
            return await orig(text, **kw)
        mirror.reply = reply
        url = "https://discord.com/channels/1/2/3"
        idx = FakeIndex([rec(1, type="file", filename="meeting.m4a", excerpt="開會錄音"),
                         rec(2, type="file", filename="song.mp3")])

        async def fetch(u): return mirror
        marks = []

        async def mark(d): marks.append(d)
        relayer = N(cfg=N(keywords=True, audio_stt_model=None, ch_screenshot=None), dc=N(fetch_by_url=fetch),
                    index=idx, dirty=N(mark=mark))
        return (lambda: AudioWorker(relayer, llm)), mirror, idx, seen, marks, url

    def test_process_summarises_marks_records_and_skips_songs(self):
        class L(FakeLLM):
            async def transcribe(self, name, data, model):
                return "歌詞啦啦啦 啦啦啦 副歌副歌副歌 重複重複重複" if name == "song.mp3" else "今天開會討論預算，決議下週五前交報告。"

            async def chat(self, prompt, data=None, **k):
                if "歌曲" in prompt and "說話" in prompt:
                    return "歌曲" if "歌詞" in (data or "") else "說話"
                if "JSON" in prompt:
                    return '{"kw": ["預算"], "gist": "開會討論預算"}'
                return "- 討論預算\n- 下週五交報告"
        mk, mirror, idx, seen, marks, url = self._setup(L())
        box = {}

        async def go():
            box["w"] = w = mk()
            return await w.process(url)
        self.assertEqual(asyncio.run(go()), "ok")
        w = box["w"]
        text = mirror.replies[0]
        self.assertIn("🎙️ meeting.m4a", text)
        self.assertIn("🎙️ song.mp3", text)
        self.assertIn("歌曲", text)
        self.assertEqual([f.filename for f in seen["kw"]["files"]], ["meeting.m4a.txt"])      # 只有說話的有逐字稿
        self.assertIsNotNone(seen["kw"]["view"])
        r1, r2 = idx.entries[(-1, 1)].rec, idx.entries[(-1, 2)].rec
        self.assertTrue(r1.audio and r2.audio)                                                  # 都標記處理過
        self.assertEqual(r1.gist, "開會討論預算")                                               # 說話的更新重點
        self.assertEqual(marks, ["2026-10-02"])                                                 # 那天要重新總結
        self.assertTrue(w.drained)
        self.assertFalse(any(w.needs(e) for e in idx.entries.values()))

    def test_no_model_leaves_records_pending(self):
        mk, mirror, idx, seen, marks, url = self._setup(FakeLLM(model=None))

        async def go():
            w = mk()
            return w, await w.process(url)
        w, res = asyncio.run(go())
        self.assertEqual(res, "nomodel")
        self.assertFalse(mirror.replies)
        self.assertTrue(all(w.needs(e) for e in idx.entries.values()))

    def test_consider_queues_only_unprocessed(self):
        mk, mirror, idx, seen, marks, url = self._setup(FakeLLM())
        import dataclasses

        async def go():
            w = mk()
            w.consider(url)
            first = (list(w.queue), w.drained)
            for e in idx.entries.values():
                e.rec = dataclasses.replace(e.rec, audio="-")
            w.queue.clear(); w._queued.clear()
            w.consider(url)
            return first, w.drained
        first, drained = asyncio.run(go())
        self.assertEqual(first, ([url], False))
        self.assertTrue(drained)
