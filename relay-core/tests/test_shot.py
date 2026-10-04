# relay-core/tests/test_shot.py
import asyncio
import dataclasses
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import records  # noqa: E402
from enrich import screenshot, shotworker  # noqa: E402
from enrich.shotworker import ShotWorker  # noqa: E402

MIRROR = "https://discord.com/channels/1/2/3"


def rec(msg, text, shot=None, reply=None):
    return records.KwRecord(-1, msg, "live", "2026-10-01 10:00", "link", MIRROR, excerpt=text, shot=shot, reply=reply)


class Index:
    def __init__(self, recs):
        self.entries = {r.key: N(rec=r, msg_id=r.tg_msg) for r in recs}
        self.updates = {}

    def by_mirror(self, url):
        return [e for e in self.entries.values() if e.rec.mirror == url]

    async def update(self, key, **kw):
        self.updates[key] = kw
        e = self.entries[key]
        e.rec = dataclasses.replace(e.rec, **kw)


class ShotTest(unittest.TestCase):
    def test_scan_queues_only_unshot_web_links(self):
        async def go():
            idx = Index([rec(1, "看 https://a.example/x"), rec(2, "沒有連結"),
                         rec(3, "https://b.example/", shot="https://discord.com/channels/1/9/9")])
            w = ShotWorker(N(ch_screenshot=5), None, idx, {})
            self.assertEqual(w.scan_index(), 1)
            self.assertEqual(list(w.queue), [MIRROR])
            self.assertEqual(w.scan_index(), 0)          # 已在佇列不重複
        asyncio.run(go())

    def test_process_backfills_and_appends_reply(self):
        async def go():
            idx = Index([rec(1, "https://a.example/x", reply="https://discord.com/channels/1/2/77")])
            edits = []

            class Msg:
                content = "https://a.example/x"
                async def edit(self, content): edits.append(content)

            async def fetch(url):
                return Msg()
            dc = N(fetch_by_url=fetch)

            async def probe(u):
                return ("", "article", None, "")

            async def post(dc_, ch, urls, when, back, links=None, **k):
                for u in urls:
                    links[u] = "https://discord.com/channels/1/5/" + "1"
                return list(links.values())
            with mock.patch.object(shotworker, "probe_link", probe), \
                 mock.patch.object(screenshot, "available", lambda: True), \
                 mock.patch.object(screenshot, "post_screenshots", post):
                w = ShotWorker(N(ch_screenshot=5), dc, idx, {})
                self.assertTrue(await w.process(MIRROR))
            self.assertEqual(idx.updates[(-1, 1)]["shot"], "https://discord.com/channels/1/5/1")
            self.assertTrue(edits and edits[0].endswith("[↗1](<https://discord.com/channels/1/5/1>)"))
        asyncio.run(go())

    def test_failure_retries_then_gives_up_this_round(self):
        async def go():
            idx = Index([rec(1, "https://a.example/x")])

            async def fetch(url):
                return N(content="https://a.example/x")

            async def probe(u):
                return ("", "article", None, "")

            async def post(*a, **k):
                return []
            with mock.patch.object(shotworker, "probe_link", probe), \
                 mock.patch.object(screenshot, "available", lambda: True), \
                 mock.patch.object(screenshot, "post_screenshots", post):
                w = ShotWorker(N(ch_screenshot=5), N(fetch_by_url=fetch), idx, {})
                results = [await w.process(MIRROR) for _ in range(shotworker.MAX_TRIES)]
            self.assertEqual(results, [False] * (shotworker.MAX_TRIES - 1) + [True])
            self.assertNotIn((-1, 1), idx.updates)
            w.scan_index()                                   # 下一輪開始：失敗的會再排進來
            self.assertEqual(list(w.queue), [MIRROR])
        asyncio.run(go())

    def test_ensure_jumps_queue_and_waits(self):
        async def go():
            idx = Index([rec(1, "https://a.example/x")])
            w = ShotWorker(N(ch_screenshot=5), None, idx, {})
            w.submit("other")
            waiter = asyncio.create_task(w.ensure(MIRROR, timeout=5))
            await asyncio.sleep(0.01)
            self.assertEqual(list(w.queue), [MIRROR, "other"])      # 插隊到最前面
            self.assertFalse(waiter.done())
            w._release(MIRROR)
            await asyncio.wait_for(waiter, 1)
            await asyncio.wait_for(ShotWorker(N(), None, Index([rec(2, "沒連結")]), {}).ensure(MIRROR), 1)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
