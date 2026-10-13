# relay-core/tests/test_album_gist.py：相簿裡每張圖的關鍵字／重點只看自己的摘要（不會把別張圖的人事物混進來）
import asyncio
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as N

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import records  # noqa: E402
try:
    from test_m4 import FakeIndex, FakeMsg, rec  # noqa: E402
except ImportError:
    from tests.test_m4 import FakeIndex, FakeMsg, rec  # noqa: E402


def jpeg(color):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(b, "JPEG")
    return b.getvalue()


class Att:
    def __init__(self, name, data):
        self.filename, self._d = name, data
        self.size, self.content_type = len(data), "image/jpeg"

    async def read(self):
        return self._d


class AlbumTest(unittest.TestCase):
    def test_each_photo_gets_only_its_own_summary_and_ocr(self):
        from enrich.pipeline import Enricher
        imgs = {"photo_1.jpg": jpeg("red"), "photo_2.jpg": jpeg("green"), "photo_3.jpg": jpeg("blue")}
        materials, captions = {}, []

        class LLM2:
            image_support = True
            memory = ""

            async def chat(self, prompt, data=None, image=None, **k):
                if image is not None:
                    captions.append(("", data or ""))
                    import base64
                    from PIL import Image
                    # prepare_image 對 JPEG 原樣回傳；用像素顏色分辨是哪張圖
                    r, g, b = Image.open(io.BytesIO(image)).convert("RGB").getpixel((5, 5))
                    return "摘要：" + ("甲說要出發" if r > 200 else "乙說在等你" if g > 100 else "丙說已經到了")
                materials.setdefault("calls", []).append(data or "")
                return '{"kw": ["x"], "gist": "g"}'
        recs = [rec(i, type="album", filename=f"photo_{i}.jpg", ocr=f"OCR文字{i}") for i in (1, 2, 3)]
        mirror = FakeMsg("# 2026-10-02\n", [Att(n, b) for n, b in imgs.items()])
        idx = FakeIndex(recs)
        dc = N(fetch_by_url=lambda u: _aret(mirror))
        cfg = N(link_summary=True, image_summary=True, document_summary=True, keywords=True, link_screenshot=False,
                ch_screenshot=None)

        async def go():
            e = Enricher(cfg, dc, LLM2(), idx, {}, None)
            await e.process(N(keys=[(-1, 1), (-1, 2), (-1, 3)], mirror_url="https://discord.com/channels/1/2/3"))
        asyncio.run(go())
        calls = materials["calls"]
        self.assertEqual(len(calls), 3)
        for i, (who_says, other1, other2) in enumerate((("甲說要出發", "乙說在等你", "丙說已經到了"),
                                                         ("乙說在等你", "甲說要出發", "丙說已經到了"),
                                                         ("丙說已經到了", "甲說要出發", "乙說在等你")), 1):
            c = next(x for x in calls if f"OCR文字{i}" in x)
            self.assertIn(who_says, c)                                       # 自己的摘要與 OCR 在
            self.assertNotIn(other1, c)                                      # 別張圖的摘要不在
            self.assertNotIn(other2, c)
        self.assertTrue(all("只有字、沒有左右位置" in cap for _, cap in captions))   # 圖片分析的 OCR 說明
        self.assertEqual(len(captions), 3)

    def test_album_redo_is_newest_first_recent_only_and_once(self):
        import tempfile
        from datetime import datetime
        from unittest import mock
        from zoneinfo import ZoneInfo
        import main as m
        today = datetime.now(ZoneInfo("Asia/Taipei"))
        from datetime import timedelta
        d = lambda n: (today - timedelta(days=n)).strftime("%Y-%m-%d 12:00")
        recs = [rec(1, mirror="M_old", date=d(200), type="album", enriched=True), rec(2, mirror="M_old", date=d(200), type="album", enriched=True),
                rec(3, mirror="M_a", date=d(10), type="album", enriched=True), rec(4, mirror="M_a", date=d(10), type="album", enriched=True),
                rec(5, mirror="M_b", date=d(1), type="album", enriched=True), rec(6, mirror="M_b", date=d(1), type="album", enriched=True),
                rec(7, mirror="M_one", date=d(1), type="image", enriched=True),                       # 只有一筆：不是相簿
                rec(8, mirror="M_new", date=d(0), type="album", enriched=False), rec(9, mirror="M_new", date=d(0), type="album", enriched=False)]
        idx = FakeIndex(recs)
        submitted, marked = [], []

        class Dirty:
            async def mark_many(self, days):
                marked.extend(days)

        async def go():
            relayer = N(index=idx, enricher=N(submit=lambda keys, mirror, front=False: submitted.append((mirror, front))), dirty=Dirty())
            with tempfile.TemporaryDirectory() as tmp, mock.patch("paths.data_dir", lambda: Path(tmp)):
                m.queue_album_redo(N(tz="Asia/Taipei", enrich_backlog=True), relayer)
                first = list(submitted)
                m.queue_album_redo(N(tz="Asia/Taipei", enrich_backlog=True), relayer)          # 只做一次
                await asyncio.sleep(0)
                return first, list(submitted)
        first, after = asyncio.run(go())
        self.assertEqual(first, [("M_a", True), ("M_b", True)])                   # 插隊；由舊到新插 → 最新的 M_b 在最前面最先做；200 天前的、單張的、還沒分析的都不排
        self.assertEqual(after, first)
        self.assertEqual(len(marked), 2)                                         # 那兩天標記重新總結

    def test_recap_prompts_carry_person_rule(self):
        import prompts
        from recap import daily
        self.assertIn(prompts.RECAP_RULE, daily.build_prompt("2026-10-12"))
        self.assertIn(prompts.RECAP_RULE, daily.with_rule("月總結的提示詞"))
        self.assertIn("你和 X 聊到", prompts.RECAP_RULE)
        self.assertIn("使用者與〈對方", prompts.CHAT_RULE)

    def test_enricher_front_jobs_run_first_and_queue_duplicate_is_skipped(self):
        from unittest import mock
        import activity
        from enrich.pipeline import Enricher

        async def go():
            e = Enricher(N(), N(), None, FakeIndex([]), {}, None)
            order = []

            async def process(job):
                order.append(job.mirror_url)
            e.process = process

            async def gate(*a):
                return None
            for m in ("A", "B", "C"):
                e.submit([(-1, 1)], m)
            e.submit([(-1, 1)], "C", front=True)                    # 插隊；一般佇列裡的 C 之後要略過
            e.submit([(-1, 1)], "D", front=True)                    # 後插的在更前面
            with mock.patch.object(activity, "gate", gate), mock.patch("lanes.get", lambda n: None):
                t = asyncio.create_task(e.run())
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if e.drained:
                        break
                t.cancel()
            return order
        self.assertEqual(asyncio.run(go()), ["D", "C", "A", "B"])


    def test_ocr_caption_explains_markers_only_when_present(self):
        from enrich.pipeline import ocr_caption
        new = ocr_caption("〔左〕你好\n〔右〕在嗎")
        self.assertIn("〔左〕〔右〕", new)
        self.assertIn("右側是使用者本人", new)
        old = ocr_caption("你好\n在嗎")
        self.assertIn("沒有左右位置", old)                                        # 舊的 OCR（沒有標記）維持原本說明
        self.assertNotIn("〔右〕", old)

    def test_ocr_clean_keeps_markers(self):
        import ocr
        self.assertEqual(ocr.clean("〔左〕你好嗎\n〔右〕我很好\n〔右〕謝謝你"), "〔左〕你好嗎\n〔右〕我很好\n〔右〕謝謝你")

    def test_chat_rule_is_added_to_image_prompt(self):
        import prompts
        from enrich.image import summarize_image
        seen = {}

        class LLM:
            image_support = True

            async def chat(self, prompt, data=None, **k):
                seen["prompt"] = prompt
                return "x"
        asyncio.run(summarize_image(LLM(), jpeg("red"), "a.jpg", ""))
        self.assertIn("畫面右側的泡泡是「使用者本人」", seen["prompt"])
        self.assertIn(prompts.CHAT_RULE, seen["prompt"])


async def _aret(x):
    return x


if __name__ == "__main__":
    unittest.main()
