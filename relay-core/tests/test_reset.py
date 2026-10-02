# relay-core/tests/test_reset.py
import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as N

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reset  # noqa: E402

BOT, ME = 1, 2


class Msg:
    def __init__(self, ch, author, content):
        self.ch, self.author, self.content = ch, N(id=author), content
        self.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)

    async def delete(self):
        self.ch.msgs.remove(self)


class Ch:
    def __init__(self, msgs):
        self.msgs = [Msg(self, a, c) for a, c in msgs]

    async def purge(self, **kw):
        return []                       # 測試資料都很舊 → 走逐則刪除

    async def history(self, limit=None, before=None):
        for m in list(self.msgs):
            yield m


def run(**flags):
    chans = {i: Ch([(BOT, "鏡像"), (ME, "我發的")]) for i in range(1, 10)}
    chans[5] = Ch([(BOT, "STATE\nx"), (BOT, "#記憶 v3"), (ME, "#記憶 手動"), (BOT, "#提示詞 v2"), (ME, "閒聊")])
    cfg = N(ch_inbox_mirror=1, ch_keywords=2, ch_recap=3, ch_screenshot=4, ch_db=5,
            ch_search=6, ch_search_test=7, ch_ai_test=8, ch_notify=9)

    async def channel(cid):
        return chans[cid]
    dc = N(client=N(user=N(id=BOT)), channel=channel)
    asyncio.run(reset.run_reset(cfg, dc, True, **flags))
    return {k: [m.content for m in v.msgs] for k, v in chans.items()}


class ResetTest(unittest.TestCase):
    def test_default(self):
        r = run(memory=False)
        self.assertEqual(r[1], [])                    # 鏡像全清
        self.assertEqual(r[2], ["我發的"])            # 關鍵字庫只清 bot 的
        self.assertEqual(r[5], ["#記憶 v3", "#記憶 手動", "#提示詞 v2", "閒聊"])
        self.assertEqual(r[6], ["鏡像", "我發的"])    # 對話保留

    def test_prompts_and_memory(self):
        r = run(memory=True, prompts=True)
        self.assertEqual(r[5], ["閒聊"])

    def test_all(self):
        r = run(memory=False, wipe_all=True)
        self.assertTrue(all(v == [] for v in r.values()))


if __name__ == "__main__":
    unittest.main()
