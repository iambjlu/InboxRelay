# relay-core/tests/test_recap.py
import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import records  # noqa: E402
from recap.runner import Recapper, make_chunks, parse_marker, strip_citations  # noqa: E402


class TestMarkers(unittest.TestCase):
    def test_chunks_and_markers(self):
        body = "\n".join(["重點" * 300] * 4)
        ch = make_chunks("DAILY", "2026-10-01", "# 📰 2026-10-01 每日總結", body, "-# 共 3 則訊息")
        self.assertGreater(len(ch), 1)
        self.assertTrue(all(len(c) <= 2000 for c in ch))
        self.assertEqual(parse_marker(ch[0]), ("DAILY", "2026-10-01", 1))
        self.assertEqual(parse_marker(ch[1]), ("DAILY", "2026-10-01", 2))
        self.assertIsNone(parse_marker("# 2026-10-01\nhello"))

    def test_strip(self):
        self.assertEqual(strip_citations("A [↗1](<https://x/1>)[↗2](<https://x/2>) B"), "A  B")


class FakeRC:
    def __init__(self):
        self.posts = {}

    def has(self, k, key):
        return (k, key) in self.posts

    def keys(self, k):
        return {key for (t, key) in self.posts if t == k}

    def first(self, k, key):
        return SimpleNamespace(content=self.posts[(k, key)][0], jump_url=f"https://discord.com/channels/1/9/{key}")

    async def publish(self, k, key, chunks):
        self.posts[(k, key)] = chunks


class FakeState:
    def __init__(self):
        self.data = {}

    async def merge(self, **kw):
        self.data.update({k: str(v) for k, v in kw.items()})


class FakeDirty:
    def __init__(self, dates=()):
        self.dates = set(dates)

    async def clear(self, d):
        self.dates.discard(d)


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, prompt, data=None, **kw):
        self.calls.append(prompt[:12])
        return "整體重點。\n### 主題\n- 事情 [1]"


def entry(day, msg, enriched=True, mirror="https://discord.com/channels/1/2/3"):
    r = records.KwRecord(tg_chat=-1, tg_msg=msg, origin="import", date=f"{day} 10:00", type="text",
                         mirror=mirror, excerpt=f"訊息{msg}", enriched=enriched)
    return SimpleNamespace(msg_id=msg, rec=r)


def make(entries, dirty=(), backlog=0):
    idx = SimpleNamespace(entries={e.rec.key: e for e in entries})
    cfg = SimpleNamespace(tz="Asia/Taipei")
    enr = SimpleNamespace(backlog=backlog)
    return Recapper(cfg, FakeLLM(), idx, FakeRC(), FakeState(), FakeDirty(dirty), enr, 12)


NOW = datetime(2026, 10, 2, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))


class TestRecapper(unittest.TestCase):
    def test_catch_up_and_month(self):
        r = make([entry("2026-09-29", 1), entry("2026-09-30", 2), entry("2026-10-01", 3), entry("2026-10-02", 4)])
        asyncio.run(r._tick(NOW))
        self.assertEqual(r.rc.keys("DAILY"), {"2026-09-29", "2026-09-30", "2026-10-01"})  # 今天不做
        self.assertEqual(r.rc.keys("MONTHLY"), {"2026-09"})                                 # 9 月完整 → 月總結
        self.assertIn("[↗1](<https://discord.com/channels/1/2/3>)", r.rc.posts[("DAILY", "2026-09-29")][0])
        self.assertTrue(r.rc.posts[("DAILY", "2026-09-29")][0].startswith("DAILY|2026-09-29\n# 📰"))
        self.assertEqual(r.state.data["summarized_until"], "2026-10-01")

    def test_month_right_after_its_last_day(self):
        r = make([entry("2026-08-30", 1), entry("2026-08-31", 2), entry("2026-09-01", 3), entry("2026-09-02", 4)])
        order = []
        orig = r.rc.publish

        async def pub(k, key, chunks):
            order.append(f"{k}:{key}")
            await orig(k, key, chunks)
        r.rc.publish = pub
        asyncio.run(r._tick(NOW))
        self.assertEqual(order, ["DAILY:2026-08-30", "DAILY:2026-08-31", "MONTHLY:2026-08",
                                 "DAILY:2026-09-01", "DAILY:2026-09-02", "MONTHLY:2026-09"])

    def test_wait_for_analysis(self):
        r = make([entry("2026-08-01", 1, enriched=False), entry("2026-10-01", 2, enriched=False)], backlog=5)
        asyncio.run(r._tick(NOW))
        self.assertEqual(r.rc.keys("DAILY"), set())        # 分析還在跑 → 都先等（昨天才過 9 小時）
        later = datetime(2026, 10, 2, 13, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        asyncio.run(r._tick(later))
        self.assertEqual(r.rc.keys("DAILY"), {"2026-10-01"})  # 最近的等滿 12 小時先出部分版本
        self.assertIn("部分內容尚未分析", r.rc.posts[("DAILY", "2026-10-01")][-1])
        self.assertIn("2026-10-01", r.state.data["partial_days"])

    def test_dirty_resummarize(self):
        r = make([entry("2026-10-01", 1)], dirty=["2026-10-01"])
        r.rc.posts[("DAILY", "2026-10-01")] = ["DAILY|2026-10-01\n舊"]
        asyncio.run(r._tick(NOW))
        self.assertNotEqual(r.rc.posts[("DAILY", "2026-10-01")][0], "DAILY|2026-10-01\n舊")
        self.assertEqual(r.dirty.dates, set())


if __name__ == "__main__":
    unittest.main()


class TestSchedule(unittest.TestCase):
    def test_seconds_until_start(self):
        r = make([])
        r.set_schedule("03:30")
        tz = ZoneInfo("Asia/Taipei")
        self.assertAlmostEqual(r.seconds_until_start(datetime(2026, 10, 2, 9, 0, tzinfo=tz)) / 3600, 18.5)
        self.assertAlmostEqual(r.seconds_until_start(datetime(2026, 10, 2, 1, 0, tzinfo=tz)) / 3600, 2.5)

    def test_plan_lists_work(self):
        r = make([entry("2026-09-29", 1), entry("2026-10-01", 2)], dirty=["2026-09-28"])
        self.assertEqual(r.plan(NOW), [("daily", "2026-09-29"), ("daily", "2026-10-01")])


class RecapPhaseTest(unittest.TestCase):
    def test_trigger_now_once(self):
        import asyncio as _a
        from recap.runner import Recapper

        async def go():
            r = Recapper.__new__(Recapper)
            r._lock, r._trigger = _a.Lock(), None
            self.assertEqual(r.phase, "")
            self.assertTrue(r.trigger_now())
            self.assertEqual(r.phase, "queued")
            self.assertFalse(r.trigger_now())          # 排隊中不能再按
            r._trigger.clear()
            async with r._lock:
                self.assertEqual(r.phase, "running")
                self.assertFalse(r.trigger_now())      # 總結中不能再按
            self.assertEqual(r.phase, "")
        _a.run(go())
