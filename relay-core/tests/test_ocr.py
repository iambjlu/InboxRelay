# relay-core/tests/test_ocr.py
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as N

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import records  # noqa: E402
from ocr import NO_IMAGE, NO_TEXT, OcrWorker, clean, match_attachments, needs_ocr, useful  # noqa: E402
import ocr as _ocr  # noqa: E402

_ocr.PACE_SEC = 0          # 測試不用等


def rec(msg, typ="image", fn="a.jpg", ocr=None, origin="live"):
    return records.KwRecord(-1, msg, origin, "2026-10-01 10:00", typ, "https://discord.com/channels/1/2/3",
                            filename=fn, ocr=ocr)


class OcrTest(unittest.TestCase):
    def test_needs_and_clean(self):
        self.assertTrue(needs_ocr(rec(1)))
        self.assertFalse(needs_ocr(rec(1, ocr=NO_TEXT)))
        self.assertFalse(needs_ocr(rec(1, typ="text", fn=None)))
        self.assertTrue(needs_ocr(rec(1, typ="file", fn="scan.PNG")))
        self.assertFalse(needs_ocr(rec(1, origin="extra")))
        self.assertEqual(clean("  \n "), NO_TEXT)
        self.assertEqual(clean("卡號 4111 1111 1111 1111 到期"), "卡號 ＊＊＊＊1111 到期")
        self.assertEqual(clean("訂單 2509011234567890"), "訂單 2509011234567890")   # 不通過 Luhn 不遮
        self.assertEqual(useful(NO_TEXT), "")

    def test_match_by_name_then_order(self):
        es = [N(rec=rec(1, fn="x.jpg")), N(rec=rec(2, fn="中文.jpg"))]
        atts = [N(filename="x.jpg", content_type="image/jpeg", url="u1"),
                N(filename="_.jpg", content_type="image/jpeg", url="u2")]
        pairs = match_attachments(es, atts)
        self.assertEqual([a.url for _, a in pairs], ["u1", "u2"])

    def test_match_normalized_missing_and_album_video(self):
        img = lambda fn, u: N(filename=fn, content_type="image/jpeg", url=u)
        es = [N(rec=rec(1, fn="photo_1@05-01-2023_10-00-00.jpg")),
              N(rec=rec(2, fn="file_2")),                       # 未匯出，鏡像內文有 📎
              N(rec=rec(3, fn="IMG 3.jpg")),
              N(rec=rec(4, typ="album", fn="video_4.mp4"))]     # 相簿裡的影片不是圖片
        atts = [img("photo_105-01-2023_10-00-00.jpg", "u1"), img("IMG_3.jpg", "u3"),
                N(filename="video_4.mp4", content_type="video/mp4", url="v")]
        pairs = match_attachments(es, atts, "內文\n📎 file_2（未匯出）")
        self.assertEqual([(e.rec.tg_msg, a and a.url) for e, a in pairs], [(1, "u1"), (2, None), (3, "u3")])
        # 檔名都對不上、但扣掉沒搬的之後數量相同 → 依順序
        es2 = [N(rec=rec(1, fn="甲.jpg")), N(rec=rec(2, fn="file_2")), N(rec=rec(3, fn="乙.jpg"))]
        pairs = match_attachments(es2, [img("a.jpg", "x"), img("b.jpg", "y")], "📎 file_2（未匯出）")
        self.assertEqual([a and a.url for _, a in pairs], ["x", None, "y"])

    def test_worker_roundtrip(self):
        async def go():
            r = rec(1)
            entry = N(rec=r, msg_id=10)
            updates = {}

            class Index:
                entries = {r.key: entry}

                def by_mirror(self, url):
                    return [entry]

                async def update(self, key, **kw):
                    updates.update(kw)
                    import dataclasses
                    entry.rec = dataclasses.replace(entry.rec, **kw)

            async def fetch(url):
                return N(attachments=[N(filename="a.jpg", content_type="image/jpeg", url="https://cdn/a.jpg")])
            sent = []
            w = OcrWorker(N(fetch_by_url=fetch), Index(), sent.append)
            task = asyncio.create_task(w.process(r.mirror))
            await asyncio.sleep(0.05)
            self.assertEqual(sent[0]["type"], "ocr_request")
            w.on_result({"id": sent[0]["id"], "text": "全聯 福利中心\n小計 320", "ms": 80})
            self.assertTrue(await task)
            self.assertEqual(updates["ocr"], "全聯 福利中心\n小計 320")
        asyncio.run(go())


class OcrVisionRetryTest(unittest.TestCase):
    def test_vision_error_retries_then_gives_up(self):
        async def go():
            r = rec(1)
            entry = N(rec=r, msg_id=10)
            updates = {}

            class Index:
                entries = {r.key: entry}

                def by_mirror(self, url):
                    return [entry]

                async def update(self, key, **kw):
                    updates.update(kw)

            async def fetch(url):
                return N(attachments=[N(filename="a.jpg", content_type="image/jpeg", url="u")])
            sent = []
            w = OcrWorker(N(fetch_by_url=fetch), Index(), sent.append)
            results = []
            for _ in range(_ocr.MAX_TRIES):
                t = asyncio.create_task(w.process(r.mirror))
                await asyncio.sleep(0.02)
                w.on_result({"id": sent[-1]["id"], "error": "boom"})
                results.append(await t)
            self.assertEqual(results, [False] * (_ocr.MAX_TRIES - 1) + [True])
            self.assertEqual(updates["ocr"], NO_IMAGE)
        asyncio.run(go())


class OcrRetryEachScanTest(unittest.TestCase):
    def test_scan_requeues_no_image_every_time(self):
        async def go():
            r = __import__("dataclasses").replace(rec(1), ocr=NO_IMAGE)
            entry = N(rec=r, msg_id=10, legacy=False)

            class Index:
                entries = {r.key: entry}

            w = OcrWorker(None, Index(), lambda d: None)
            for _ in range(2):
                self.assertEqual(w.scan_index(), 1)
                self.assertEqual(list(w.queue), [r.mirror])
                self.assertIn(r.key, w._retry_keys)
                w.queue.clear()
                w._queued.clear()
        asyncio.run(go())


class OcrDirtyTest(unittest.TestCase):
    def test_marks_summarized_day(self):
        async def go():
            r = rec(1)
            entry = N(rec=r, msg_id=10)

            class Index:
                entries = {r.key: entry}

                def by_mirror(self, url):
                    return [entry]

                async def update(self, key, **kw):
                    import dataclasses
                    entry.rec = dataclasses.replace(entry.rec, **kw)

            async def fetch(url):
                return N(attachments=[N(filename="a.jpg", content_type="image/jpeg", url="u")])
            sent, marked = [], []

            async def mark(days):
                marked.extend(days)
            w = OcrWorker(N(fetch_by_url=fetch), Index(), sent.append)
            w.has_daily = lambda d: d == "2026-10-01"
            w.mark_days = mark
            t = asyncio.create_task(w.process(r.mirror))
            await asyncio.sleep(0.05)
            w.on_result({"id": sent[0]["id"], "text": "發票 全聯 320 元"})
            await t
            await w._flush_dirty()
            self.assertEqual(marked, ["2026-10-01"])
        asyncio.run(go())


class OcrRetryTest(unittest.TestCase):
    def test_transient_download_error_is_not_permanent(self):
        async def go():
            import ocr as ocr_mod
            ocr_mod.PACE_SEC = 0
            r = rec(1)
            entry = N(rec=r, msg_id=10)
            updates = []

            class Index:
                entries = {r.key: entry}

                def by_mirror(self, url):
                    return [entry]

                async def update(self, key, **kw):
                    updates.append(kw)

            async def fetch(url):
                return N(attachments=[N(filename="a.jpg", content_type="image/jpeg", url="u")])
            sent = []
            w = OcrWorker(N(fetch_by_url=fetch), Index(), sent.append)
            t = asyncio.create_task(w.process(r.mirror))
            await asyncio.sleep(0.05)
            w.on_result({"id": sent[0]["id"], "error": "下載失敗：HTTP 429", "retry": True, "retry_after": 60})
            self.assertFalse(await t)                  # 暫時失敗 → 回報要重試
            self.assertEqual(updates, [])              # 不會寫上「無法取得圖片」
            self.assertEqual(w._backoff, 60)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
