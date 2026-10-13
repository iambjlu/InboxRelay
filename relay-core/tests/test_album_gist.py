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
