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
        self.pending = set()
        self.placed = []          # 佔位的發文順序

    def has(self, k, key):
        return (k, key) in self.posts and (k, key) not in self.pending

    def exists(self, k, key):
        return (k, key) in self.posts

    def keys(self, k):
        return {key for (t, key) in self.posts if t == k and (t, key) not in self.pending}

    def first(self, k, key):
        return SimpleNamespace(content=self.posts[(k, key)][0], jump_url=f"https://discord.com/channels/1/9/{key}")

    async def placeholder(self, k, key, text):
        self.posts[(k, key)] = [text]
        self.pending.add((k, key))
        self.placed.append(f"{k}:{key}")

    async def publish(self, k, key, chunks):
        self.posts[(k, key)] = chunks
        self.pending.discard((k, key))

    async def publish_post(self, k, key, title, body, footer):
        from recap.runner import make_chunks_fit
        await self.publish(k, key, make_chunks_fit(k, key, title, body, footer))


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

    def test_year_after_december(self):
        r = make([entry("2025-11-30", 1), entry("2025-12-01", 2), entry("2026-01-02", 3)])
        order = []
        orig = r.rc.publish

        async def pub(k, key, chunks):
            order.append(f"{k}:{key}")
            await orig(k, key, chunks)
        r.rc.publish = pub
        asyncio.run(r._tick(NOW))
        self.assertEqual(order, ["DAILY:2025-11-30", "MONTHLY:2025-11", "DAILY:2025-12-01", "MONTHLY:2025-12",
                                 "YEARLY:2025", "DAILY:2026-01-02", "MONTHLY:2026-01"])   # 今年（2026）不做年總結
        self.assertTrue(r.rc.posts[("YEARLY", "2025")][0].startswith("YEARLY|2025\n# 🎆 2025 年總結"))

    def test_cycle_waits_for_analysis_then_in_order(self):
        r = make([entry("2025-12-30", 1), entry("2026-01-02", 2, enriched=False), entry("2026-01-03", 3)], backlog=1)
        order = []
        orig = r.rc.publish

        async def pub(k, key, chunks):
            order.append(f"{k}:{key}")
            await orig(k, key, chunks)
        r.rc.publish = pub
        r.enricher.drained = False

        import recap.runner as rr
        rr.DRAIN_POLL_SEC = 0.01

        async def go():
            t = asyncio.create_task(r.cycle())
            await asyncio.sleep(0.05)
            self.assertEqual(order, [])                 # 分析還沒做完：一篇都不發
            for e in r.index.entries.values():
                e.rec.enriched = True
            r.enricher.drained, r.enricher.backlog = True, 0
            await asyncio.wait_for(t, 5)
        asyncio.run(go())
        self.assertEqual(order, ["DAILY:2025-12-30", "MONTHLY:2025-12", "YEARLY:2025",
                                 "DAILY:2026-01-02", "DAILY:2026-01-03", "MONTHLY:2026-01"])

    def test_make_post_fits_one_message(self):
        from recap.runner import make_post
        body = "\n".join(f"- 重點{i} [↗{i}](<https://discord.com/channels/100000000000000000/200000000000000000/{i:019d}>)"
                          for i in range(1, 40))
        post = make_post("DAILY", "2026-10-01", "# 📰 標題", body, "-# 共 39 則訊息")
        self.assertLessEqual(len(post), 1990)
        self.assertTrue(post.startswith("DAILY|2026-10-01\n# 📰 標題"))
        self.assertIn("[↗1](<", post)                          # 前面的連結保留，後面的改成純編號

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


class SplitTest(unittest.TestCase):
    URL = "https://discord.com/channels/519696701917757440/1555324160195166300/"

    def _body(self, n=30):
        return "\n".join(f"- 第 {i} 個重點，說明一些內容 " + "".join(f"[↗{j}](<{self.URL}{1000 + j}>)" for j in (i, i + 1, i + 2))
                         for i in range(1, n + 1))

    def test_long_recap_split_keeps_every_link(self):
        from recap.runner import make_chunks_fit
        body = self._body()
        chunks = make_chunks_fit("DAILY", "2022-03-03", "# 📰 標題", body, "-# 共 30 則訊息")
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 2000 for c in chunks))
        self.assertEqual(sum(c.count("[↗") for c in chunks), body.count("[↗"))      # 連結一個都沒少
        self.assertEqual([parse_marker(c) for c in chunks][0], ("DAILY", "2022-03-03", 1))
        self.assertEqual([parse_marker(c)[2] for c in chunks], list(range(1, len(chunks) + 1)))
        self.assertTrue(chunks[1].startswith("-# DAILY|2022-03-03 2/"))

    def test_fixed_chunk_count_falls_back_to_plain_numbers(self):
        from recap.runner import make_chunks_fit
        body = self._body()
        chunks = make_chunks_fit("DAILY", "2022-03-03", "# 📰 標題", body, "-# 共 30 則訊息", max_chunks=1)
        self.assertEqual(len(chunks), 1)
        self.assertLessEqual(len(chunks[0]), 2000)
        self.assertIn("↗", chunks[0])                                                  # 後面變成純文字編號
        self.assertLess(chunks[0].count("[↗"), body.count("[↗"))

    def test_channel_tail_body_and_publish_post(self):
        import asyncio
        from types import SimpleNamespace as N
        from recap.runner import RecapChannel
        sent, edited = [], []

        class Msg:
            def __init__(self, i, content):
                self.id, self.content = i, content
                self.jump_url = f"https://discord.com/channels/1/9/{i}"

            async def edit(self, content, suppress=True):
                self.content = content
                edited.append(self.id)
                return self

            async def delete(self):
                pass

        class Ch:
            async def send(self, content, suppress_embeds=True):
                m = Msg(100 + len(sent), content)
                sent.append(m)
                return m

        async def go():
            rc = RecapChannel(Ch(), 1)
            rc.posts[("DAILY", "A")] = {1: Msg(10, "DAILY|A\n# 標題\n- a")}
            rc.posts[("DAILY", "B")] = {1: Msg(20, "DAILY|B\n# 標題\n- b")}
            self.assertFalse(rc.is_tail("DAILY", "A"))
            self.assertTrue(rc.is_tail("DAILY", "B"))
            body = self._body()
            await rc.publish_post("DAILY", "A", "# 標題", body, "-# f")                 # 中間那篇：維持 1 則（連結縮成編號）
            self.assertEqual(len(rc.posts[("DAILY", "A")]), 1)
            self.assertEqual(sent, [])
            await rc.publish_post("DAILY", "B", "# 標題", body, "-# f")                 # 最後一篇：拆成多則、接在後面
            self.assertGreater(len(rc.posts[("DAILY", "B")]), 1)
            self.assertEqual(len(sent), len(rc.posts[("DAILY", "B")]) - 1)
            text = rc.body("DAILY", "B")
            self.assertEqual(text.count("[↗"), body.count("[↗"))                        # 內文接起來，連結都在
            await rc.publish_post("DAILY", "C", "# 標題", body, "-# f")                 # 新的一篇：直接拆
            self.assertTrue(rc.is_tail("DAILY", "C"))
        asyncio.run(go())


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
            import lanes
            from types import SimpleNamespace as N
            lanes.setup(N(llm_schedule="daily", recap_time="03:30", llm_end="", ane_schedule="always", shot_schedule="always", shot_start="03:30", shot_end="",
                          ane_start="03:30", ane_end="", tz="Asia/Taipei"))
            r = Recapper.__new__(Recapper)
            r._lock, r._trigger = _a.Lock(), None
            self.assertEqual(r.phase, "")
            self.assertTrue(r.trigger_now())
            self.assertEqual(r.phase, "queued")
            self.assertFalse(r.trigger_now())          # 排隊中不能再按
            lanes.get("llm")._trigger.clear()
            async with r._lock:
                self.assertEqual(r.phase, "running")
                self.assertFalse(r.trigger_now())      # 總結中不能再按
            self.assertEqual(r.phase, "")
        _a.run(go())
