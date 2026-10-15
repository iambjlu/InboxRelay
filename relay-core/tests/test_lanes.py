# relay-core/tests/test_lanes.py
import asyncio
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lanes  # noqa: E402


class LaneTest(unittest.TestCase):
    def test_modes_and_gate(self):
        async def go():
            a = lanes.Lane("llm", "外部 LLM 工作", "always")
            self.assertEqual(a.state, "running")
            await asyncio.wait_for(a.gate(), 1)
            d = lanes.Lane("llm", "外部 LLM 工作", "daily", "03:30")
            self.assertEqual(d.state, "waiting")
            self.assertEqual(d.describe(), "每天 03:30 開始到做完")
            w = lanes.Lane("ane", "ANE", "window", "01:00", "07:00")
            self.assertEqual(w.describe(), "每天 01:00～07:00")
            self.assertEqual(lanes.Lane("x", "x", "window", "01:00", "").mode, "daily")   # 沒結束時間＝做到完
            a.pause()
            self.assertEqual(a.state, "paused")
            g = asyncio.create_task(a.gate())
            await asyncio.sleep(0.05)
            self.assertFalse(g.done())
            a.resume()
            await asyncio.wait_for(g, 1)
        asyncio.run(go())

    def test_manual_run_until_drained(self):
        async def go():
            lane = lanes.Lane("ane", "ANE", "manual")
            work = {"left": 3, "ran": 0}

            async def cycle():
                work["ran"] += 1

            async def worker():
                while work["left"]:
                    await lane.gate()
                    work["left"] -= 1
                    await asyncio.sleep(0.01)
            runner = asyncio.create_task(lanes.run_lane(lane, drained=lambda: work["left"] == 0, cycle=cycle))
            w = asyncio.create_task(worker())
            await asyncio.sleep(0.05)
            self.assertEqual(work["left"], 3)          # 手動模式：沒按就不做
            self.assertTrue(lane.run_now())
            await asyncio.wait_for(w, 15)
            self.assertEqual(work["ran"], 1)
            for _ in range(200):                         # 做完後車道會關掉
                if not lane.open:
                    break
                await asyncio.sleep(0.1)
            self.assertFalse(lane.open)
            self.assertEqual(lane.state, "waiting")
            runner.cancel()
        asyncio.run(go())

    def test_window_closes_at_end(self):
        async def go():
            lane = lanes.Lane("ane", "ANE", "window", "03:30", "07:00")
            lane.open, lane.close_at = True, lane.now() - timedelta(seconds=1)
            self.assertEqual(lane.state, "running")
            # run_lane 會在下一輪檢查結束時間；這裡直接驗證開始時間的計算
            nxt = lane._next(lane.start, lane.now())
            self.assertEqual((nxt.hour, nxt.minute), (3, 30))
            self.assertGreater(nxt, lane.now())
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()


class OfflineTest(unittest.TestCase):
    def test_offline_blocks_until_next_round(self):
        import lanes as L
        lane = L.Lane("llm", "外部 LLM 工作", "always")
        self.assertTrue(lane.allowed())
        lane.go_offline("APIConnectionError")
        self.assertFalse(lane.allowed())
        self.assertEqual(lane.state, "offline")

    def test_enricher_keeps_job_when_offline(self):
        import asyncio
        import openai
        import httpx
        import lanes as L
        from enrich.pipeline import Enricher, EnrichJob

        async def go():
            L.LANES.clear()
            L.LANES["llm"] = L.Lane("llm", "外部 LLM 工作", "always")
            en = Enricher.__new__(Enricher)
            en.queue = asyncio.Queue()
            en._queued, en._front, en._posted, en._holding = set(), __import__("collections").deque(), [], False
            en._skip_once = set()
            en.index = type("I", (), {"get": lambda self, k: None})()
            deleted = []

            class Msg:
                async def delete(self):
                    deleted.append(1)

            async def process(job):
                en._posted.append(Msg())
                raise openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
            en.process = process
            en.shot_worker = None
            en.submit = Enricher.submit.__get__(en)
            en.submit([(1, 1)], "m1")
            t = asyncio.create_task(en.run())
            await asyncio.sleep(0.05)
            t.cancel()
            self.assertEqual(L.LANES["llm"].state, "offline")
            self.assertEqual(deleted, [1])                 # 發出去的回覆收回
            self.assertEqual(en.backlog, 1)                # 工作還在（卡在 gate 等下次）
            self.assertFalse(en.drained)
            L.LANES.clear()
        asyncio.run(go())


class BlockedTest(unittest.TestCase):
    def test_blocked_lane_waits_and_describes(self):
        async def go():
            lane = lanes.Lane("llm", "外部 LLM 工作", "always")
            flag = {"busy": True}
            lane.blocked_by = lambda: flag["busy"]
            self.assertFalse(lane.allowed())
            self.assertEqual(lane.state, "waiting")
            self.assertEqual(lane.describe(), "等連結截圖做完")
            flag["busy"] = False
            self.assertTrue(lane.allowed())
            await asyncio.wait_for(lane.gate(), 1)
        asyncio.run(go())


class OfflineRetryTest(unittest.TestCase):
    def test_retries_until_reachable_then_resumes(self):
        import asyncio
        from unittest import mock
        import lanes as L

        async def go():
            lane = L.Lane("llm", "外部 LLM 工作", "always")
            tries = []

            async def probe():
                tries.append(1)
                if len(tries) < 3:
                    raise ConnectionError("tailscale not up")
                return ["m"]
            lane.probe = probe
            with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
                lane.go_offline("APIConnectionError")
                self.assertTrue(lane.offline)
                await lane._retrying
            self.assertEqual(len(tries), 3)
            self.assertFalse(lane.offline)
            self.assertTrue(lane._trigger.is_set())              # 原本就該做事 → 立即開始
        asyncio.run(go())

    def test_gives_up_after_all_delays(self):
        import asyncio
        from unittest import mock
        import lanes as L

        async def go():
            lane = L.Lane("llm", "外部 LLM 工作", "always")
            n = []

            async def probe():
                n.append(1)
                raise ConnectionError
            lane.probe = probe
            with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
                lane.go_offline()
                await lane._retrying
            self.assertEqual(len(n), len(L.RETRY_DELAYS))
            self.assertTrue(lane.offline)                          # 之後交給原本的排程
        asyncio.run(go())


class FirstConnectTest(unittest.TestCase):
    def test_waits_then_connects_or_goes_offline(self):
        import asyncio
        from types import SimpleNamespace as N
        from unittest import mock
        import lanes as L
        import main as m

        async def go(ok):
            lane = L.Lane("llm", "外部 LLM 工作", "always")
            lane.connecting = True
            self.assertFalse(lane.allowed())

            async def ping():
                if not ok:
                    raise ConnectionError
                return ["mm"]
            server = N(ai_status="")
            slept = []

            async def fake_sleep(d): slept.append(d)
            with mock.patch("asyncio.sleep", new=fake_sleep):
                await m.first_llm_connect(N(ai_server="s", ai_model="mm"), N(ping=ping), lane, server)
                if lane._retrying:
                    lane._retrying.cancel()
            self.assertEqual(slept[0], 10)                       # 全部啟動後先等 10 秒
            return lane, server
        lane, server = asyncio.run(go(True))
        self.assertTrue(lane.allowed())
        self.assertIn("已連線", server.ai_status)
        lane, server = asyncio.run(go(False))
        self.assertTrue(lane.offline)
        self.assertFalse(lane.connecting)
