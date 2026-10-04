# relay-core/tests/test_ocr_wait.py：AI 分析等 OCR、辨識結果背景寫入、舊分析沒有 OCR 時的重新分析標記
import asyncio
import dataclasses
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ocr  # noqa: E402
import records  # noqa: E402

MIRROR = "https://discord.com/channels/1/2/3"


def rec(msg, **kw):
    return records.KwRecord(-1, msg, "import", "2025-07-16 13:12", "image", MIRROR, filename=f"p{msg}.jpg", **kw)


class Index:
    def __init__(self, recs):
        self.entries = {r.key: N(rec=r, msg_id=r.tg_msg, legacy=False) for r in recs}
        self.log = []

    def by_mirror(self, url):
        return [e for e in self.entries.values() if e.rec.mirror == url]

    def get(self, key):
        return self.entries.get(key)

    async def update(self, key, **kw):
        await asyncio.sleep(0.01)
        self.log.append((key, kw))
        e = self.entries[key]
        e.rec = dataclasses.replace(e.rec, **kw)


class OcrWaitTest(unittest.TestCase):
    def test_record_marker_roundtrip(self):
        r = rec(1, enriched=True, enriched_ocr=True)
        self.assertIn("enriched: ocr", records.format_kw(r))
        back = records.parse_record(records.format_kw(r))
        self.assertTrue(back.enriched and back.enriched_ocr)
        old = records.parse_record(records.format_kw(rec(2, enriched=True)))
        self.assertTrue(old.enriched and not old.enriched_ocr)               # 舊的 yes＝分析時可能沒有 OCR
        self.assertTrue(records.parse_record(records.format_kw(rec(3))).enriched is False)

    def test_ensure_waits_until_mirror_done_only_when_lane_running(self):
        async def go():
            idx = Index([rec(1), rec(2, ocr="文字")])
            w = ocr.OcrWorker(None, idx, lambda d: None)
            with mock.patch("lanes.get", lambda n: N(state="paused")):
                await asyncio.wait_for(w.ensure(MIRROR), 1)                   # 車道沒在跑 → 不等
            with mock.patch("lanes.get", lambda n: N(state="running")):
                task = asyncio.create_task(w.ensure(MIRROR, timeout=5))
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())                                 # 還有圖沒辨識 → 等
                self.assertEqual(list(w.queue), [MIRROR])                     # 而且插隊排進去
                w._release(MIRROR)
                await asyncio.wait_for(task, 1)
            idx2 = Index([rec(1, ocr="有字")])
            w2 = ocr.OcrWorker(None, idx2, lambda d: None)
            with mock.patch("lanes.get", lambda n: N(state="running")):
                await asyncio.wait_for(w2.ensure(MIRROR), 1)                  # 都辨識完 → 不等
        asyncio.run(go())

    def test_write_in_background_and_trigger_reanalysis(self):
        async def go():
            r = rec(1, enriched=True)
            idx = Index([r])
            w = ocr.OcrWorker(None, idx, lambda d: None)
            hits = []
            w.on_text = lambda e: hits.append(e.rec.key)
            await w._write(r.key, ocr="圖中有字")
            self.assertEqual(idx.log, [])                                      # 寫入在背景，辨識不用等
            self.assertFalse(w.drained)
            await w.flush()
            self.assertEqual(idx.log[0][1]["ocr"], "圖中有字")
            self.assertEqual(hits, [r.key])                                    # 之前分析沒有 OCR → 通知重新分析
            self.assertTrue(w.drained)
            await w._write(r.key, ocr=ocr.NO_TEXT)
            await w.flush()
            self.assertEqual(hits, [r.key])                                    # 沒有字就不用重做
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
