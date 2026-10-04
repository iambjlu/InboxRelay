# relay-core/tests/test_link_image.py
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as N

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import records  # noqa: E402
import ocr as _ocr  # noqa: E402
from enrich.link import embed_preview, summarize_probed  # noqa: E402
from enrich.refresh import join_fit, needs_refresh, parse_segments  # noqa: E402

_ocr.PACE_SEC = 0
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


class FakeLLM:
    image_support = True

    def __init__(self):
        self.calls = []

    async def chat(self, prompt, data=None, image=None, image_mime="image/jpeg", max_tokens=800, **k):
        self.calls.append((prompt, data, image, image_mime))
        return "- 重點"


class LinkImageTest(unittest.TestCase):
    def test_image_link_is_described(self):
        llm = FakeLLM()
        r = asyncio.run(summarize_probed(llm, "https://x/a", ("note", "image", PNG, "")))
        self.assertEqual(r.title, "🖼️ https://x/a")
        self.assertIs(r.image, PNG)
        self.assertEqual(llm.calls[0][3], "image/png")

    def test_fallback_uses_preview_and_screenshot(self):
        llm = FakeLLM()
        r = asyncio.run(summarize_probed(llm, "https://x/p", ("", "", None, "ConnectError"),
                                         preview="標題：某商品", shot=PNG))
        self.assertFalse(r.failed)
        self.assertIn("網頁預覽：", llm.calls[0][1])
        self.assertIs(llm.calls[0][2], PNG)
        self.assertTrue(r.summary.endswith("（根據網頁預覽、截圖）"))

    def test_nothing_available_still_fails(self):
        r = asyncio.run(summarize_probed(FakeLLM(), "https://x/p", ("", "", None, "ConnectError")))
        self.assertTrue(r.failed)
        self.assertEqual(r.summary, "無法讀取（ConnectError）")

    def test_embed_preview(self):
        e = N(url="https://x/p/", title="標題", description="描述", provider=N(name="網站A"), author=None)
        m = N(embeds=[e], content="看 https://x/p")
        self.assertEqual(embed_preview(m, "https://x/p"), "網站：網站A\n標題：標題\n描述：描述")
        self.assertEqual(embed_preview(N(embeds=[], content=""), "https://x/p"), "")

    def test_record_roundtrip(self):
        r = records.KwRecord(-1, 5, "live", "2026-10-04 10:00", "link", "m", img="https://x/a.jpg")
        self.assertEqual(records.parse_record(records.format_kw(r)).img, "https://x/a.jpg")


class RefreshTest(unittest.TestCase):
    def test_segments_and_fit(self):
        c = "**🔗 https://a**\n> 無法讀取\n**🖼️ b.jpg**\n> - 圖\n📸 截圖：[↗1](<u>)"
        segs = parse_segments(c)
        bad = [s for s in segs if s["url"] and needs_refresh("\n".join(s["lines"][1:]))]
        self.assertEqual([s["url"] for s in bad], ["https://a"])
        self.assertEqual(join_fit(segs, []), c)
        bad[0]["lines"] = ["**🔗 https://a**"] + ["> " + "字" * 100] * 30
        out = join_fit(segs, bad)
        self.assertLessEqual(len(out), 2000)
        self.assertTrue(out.endswith("📸 截圖：[↗1](<u>)"))
        self.assertTrue(needs_refresh("> - 這是檔案下載連結\n> - 類型：image/jpeg｜大小：1 KB"))


class OcrLinkImageTest(unittest.TestCase):
    def test_worker_downloads_link_image(self):
        async def go():
            r = records.KwRecord(-1, 7, "live", "2026-10-04 10:00", "link", "https://discord.com/channels/1/2/3",
                                 img="https://x/a.jpg")
            entry = N(rec=r, msg_id=1)
            updates = {}

            class Index:
                entries = {r.key: entry}

                def by_mirror(self, url):
                    return [entry]

                async def update(self, key, **kw):
                    updates.update(kw)

            async def fetch(url):
                raise AssertionError("網址圖片不用讀鏡像")
            sent = []
            w = _ocr.OcrWorker(N(fetch_by_url=fetch), Index(), sent.append)
            self.assertTrue(_ocr.needs_ocr(r))
            t = asyncio.create_task(w.process(r.mirror))
            await asyncio.sleep(0.02)
            self.assertEqual(sent[0]["url"], "https://x/a.jpg")
            w.on_result({"id": sent[0]["id"], "text": "菜單 拿鐵 65"})
            self.assertTrue(await t)
            self.assertEqual(updates["ocr"], "菜單 拿鐵 65")
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
