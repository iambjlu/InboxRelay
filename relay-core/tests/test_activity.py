# relay-core/tests/test_activity.py
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import activity  # noqa: E402


class FakeMsg:
    def __init__(self, ch, content):
        self.ch, self.content, self.deleted = ch, content, False

    async def edit(self, content):
        self.content = content

    async def delete(self):
        self.deleted = True
        self.ch.alive.remove(self)


class FakeCh:
    def __init__(self):
        self.alive = []

    async def send(self, content):
        m = FakeMsg(self, content)
        self.alive.append(m)
        return m


class TestActivity(unittest.TestCase):
    def test_progress_line(self):
        t = activity.Task(1, "AI 分析", total=200, done=50, detail="2026-10-02")
        self.assertEqual(t.line(), "AI 分析 50/200（25.0%）｜2026-10-02")

    def test_discord_status_created_updated_deleted(self):
        async def go():
            act = activity.Activity()
            ch = FakeCh()
            old = activity.DISCORD_AFTER_SEC, activity.DISCORD_EVERY_SEC
            activity.DISCORD_AFTER_SEC, activity.DISCORD_EVERY_SEC = 0, 0
            try:
                t = act.start("每日總結", total=3)
                await act._discord(ch)
                self.assertEqual(len(ch.alive), 1)
                self.assertIn("每日總結 0/3（0.0%）", ch.alive[0].content)
                t.advance(detail="產生 2026-10-01")
                await act._discord(ch)
                self.assertIn("1/3（33.3%）｜產生 2026-10-01", ch.alive[0].content)
                act.finish(t)
                await act._discord(ch)
                self.assertEqual(ch.alive, [])          # 做完就刪掉
            finally:
                activity.DISCORD_AFTER_SEC, activity.DISCORD_EVERY_SEC = old
        asyncio.run(go())

    def test_pause_blocks_gate(self):
        async def go():
            import lanes
            from types import SimpleNamespace as N
            lanes.setup(N(llm_schedule="always", recap_time="03:30", llm_end="", ane_schedule="always",
                          ane_start="03:30", ane_end="", tz="Asia/Taipei"))
            act = activity.Activity()
            act.pause()
            waiter = asyncio.create_task(act.gate())
            await asyncio.sleep(0.05)
            self.assertFalse(waiter.done())
            act.resume()
            await asyncio.wait_for(waiter, 1)
            self.assertIn("⏸ 外部 LLM 工作已暫停", (act.pause(), act.status_lines())[1][0])
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()


class TestStatusBar(unittest.TestCase):
    def test_fit_width(self):
        import statusbar as sb
        self.assertEqual(sb.char_width("中"), 2)
        self.assertEqual(sb.char_width("a"), 1)
        out = sb.fit("運行中運行中運行中", 9)
        self.assertLessEqual(sum(sb.char_width(c) for c in out), 8)

    def test_disabled_when_not_tty(self):
        import io
        import statusbar as sb
        self.assertFalse(sb.StatusBar(lambda: [], stream=io.StringIO()).start())


class TestIpc(unittest.TestCase):
    def test_status_and_commands(self):
        import json
        from types import SimpleNamespace as N
        import ipc

        async def go():
            import lanes
            lanes.setup(N(llm_schedule="always", recap_time="03:30", llm_end="", ane_schedule="always",
                          ane_start="03:30", ane_end="", tz="Asia/Taipei"))
            act = activity.Activity()
            t = act.start("每日總結", total=4)
            t.advance(detail="產生 2026-10-01")
            stop = asyncio.Event()
            triggered = []
            rc = N(state=N(data={"summarized_until": "2026-09-30"}), seconds_until_start=lambda: 3600,
                   trigger_now=lambda: triggered.append(1) or True, phase="")
            srv = ipc.IpcServer(act, N(enricher=N(backlog=7), prober=None), lambda: rc,
                                N(chat_title="hello world"), N(recap_time="03:30"), "✅ 已連線", stop)
            st = srv.status()
            json.dumps(st, ensure_ascii=False)
            self.assertEqual((st["backlog"], st["summarized_until"], st["next_recap_hours"]), (7, "2026-09-30", 1.0))
            self.assertEqual(st["tasks"][0]["line"], "每日總結 1/4（25.0%）｜產生 2026-10-01")
            srv.handle('{"type": "pause"}')
            self.assertTrue(act.paused)
            srv.handle('{"type": "resume"}')
            self.assertFalse(act.paused)
            srv.handle('{"type": "recap_now"}')
            self.assertEqual(triggered, [1])
            srv.handle('{"type": "stop"}')
            self.assertTrue(stop.is_set())
        asyncio.run(go())


class TestMemGuard(unittest.TestCase):
    def test_trim_and_restart_when_managed(self):
        import memguard
        cleared, notes = [], []
        stop = None

        async def go():
            nonlocal stop
            stop = asyncio.Event()

            async def note(t):
                notes.append(t)
            g = memguard.MemGuard(soft_mb=1, hard_mb=1, trims=[lambda: cleared.append(1)], notify=note,
                                  managed_by_app=True, stop_event=stop)
            await asyncio.wait_for(g.loop(every=0), timeout=5)
        asyncio.run(go())
        self.assertGreaterEqual(len(cleared), 3)        # 每次超過軟上限都清快取
        self.assertTrue(stop.is_set())                  # 連續 3 次超過硬上限 → 請 App 重啟
        self.assertIn("自動重新啟動", notes[0])


class TestHostLock(unittest.TestCase):
    def test_other_host(self):
        from datetime import datetime, timedelta, timezone
        import state
        now = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
        rec = {"status": "running", "host": "macbook", "heartbeat": (now - timedelta(minutes=1)).isoformat()}
        self.assertEqual(state.other_host_active(rec, "mini", now), "macbook")
        self.assertIsNone(state.other_host_active(rec, "macbook", now))                    # 自己（當機重開）
        old = dict(rec, heartbeat=(now - timedelta(minutes=5)).isoformat())
        self.assertIsNone(state.other_host_active(old, "mini", now))                       # 對方停了 → 可接手
        self.assertIsNone(state.other_host_active(dict(rec, status="stopped"), "mini", now))


class TestFineProgress(unittest.TestCase):
    def test_item_steps_and_llm(self):
        t = activity.Task(1, "AI 分析", total=4)
        self.assertEqual(t.progress, 0)
        t.begin_item(4)                 # 這則分 4 步
        t.step(); t.step()
        t.set_llm(0.5)                  # 第 3 步的 AI 回覆產生一半
        self.assertAlmostEqual(t.progress, (2.5 / 4) / 4)
        self.assertIn("15.6%", t.line())
        t.advance()
        self.assertAlmostEqual(t.progress, 0.25)
        t.set(done=4)
        self.assertEqual(t.progress, 1.0)

    def test_stream_reports(self):
        import llm as llm_mod
        from types import SimpleNamespace as N

        class Stream:
            def __init__(self, n):
                self.items = [N(choices=[N(delta=N(content="字"))]) for _ in range(n)]

            def __aiter__(self):
                self.i = 0
                return self

            async def __anext__(self):
                if self.i >= len(self.items):
                    raise StopAsyncIteration
                self.i += 1
                return self.items[self.i - 1]

        seen = []

        class T:
            def set_llm(self, f):
                seen.append(f)

        async def create(**kw):
            self.assertTrue(kw.get("stream"))
            return Stream(40)

        L = llm_mod.LLM.__new__(llm_mod.LLM)
        L._avg_tokens = {}
        L.client = N(chat=N(completions=N(create=create)))
        out = asyncio.run(L._stream({}, T(), 200))
        self.assertEqual(out, "字" * 40)
        self.assertGreater(len(seen), 5)
        self.assertEqual(seen[-1], 0.95)
        self.assertAlmostEqual(L._avg_tokens[200], 80 * 0.7 + 40 * 0.3)
