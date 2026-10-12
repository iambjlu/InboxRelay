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


class QueueOrderTest(unittest.TestCase):
    def _worker(self, order, fail_first=()):
        idx = Index([])
        w = ocr.OcrWorker(None, idx, lambda d: None)
        w._backoff = 0.01
        seen = {}

        async def process(m):
            order.append(m)
            if m in fail_first and not seen.get(m):
                seen[m] = True
                w._why = "測試：App 沒有回傳"
                return False
            return True
        w.process = process

        async def gate(name):
            return None
        return w, gate

    def test_failing_mirror_goes_to_back_and_does_not_block_others(self):
        async def go():
            import activity
            order = []
            w, gate = self._worker(order, fail_first={"A"})
            with mock.patch.object(activity, "gate", gate):
                for m in ("A", "B", "C"):
                    w.submit(m)
                t = asyncio.create_task(w.run())
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if w.drained:
                        break
                t.cancel()
            self.assertEqual(order, ["A", "B", "C", "A"])               # A 失敗後排到最後，B、C 不用等它
        asyncio.run(go())

    def test_single_failing_mirror_waits_backoff_and_shows_reason(self):
        async def go():
            import activity
            order = []
            w, gate = self._worker(order, fail_first={"A"})
            details = []
            real = activity.Activity()
            with mock.patch.object(activity, "current", real), mock.patch.object(activity, "gate", gate):
                w.submit("A")
                t = asyncio.create_task(w.run())
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if w._task is not None and "暫時失敗" in w._task.detail:
                        details.append(w._task.detail)
                    if w.drained and order.count("A") >= 2:
                        break
                t.cancel()
            self.assertEqual(order[:2], ["A", "A"])
            self.assertTrue(details and "測試：App 沒有回傳" in details[0])    # 儀表板看得到在等什麼
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
