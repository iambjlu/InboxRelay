# relay-core/tests/test_summaryhome.py
import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as N
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from enrich import summaryhome as sh  # noqa: E402

MIRROR_CH, PREVIEW_CH = 10, 20


class Msg:
    def __init__(self, id, ch, content, author=1, reference=None):
        self.id, self.content, self.author, self.reference, self.attachments = id, content, N(id=author), reference, []
        self.channel = N(id=ch)
        self.jump_url = f"https://discord.com/channels/1/{ch}/{id}"
        self.deleted = False

    async def edit(self, content):
        self.content = content

    async def delete(self):
        self.deleted = True


class SummaryHomeTest(unittest.TestCase):
    def test_put_arrow_idempotent(self):
        a = sh.put_arrow("# 2026-10-17\n看這個 https://x.com", "https://discord.com/channels/1/20/5")
        self.assertTrue(a.endswith("-# [↗ 摘要](<https://discord.com/channels/1/20/5>)"))
        b = sh.put_arrow(a, "https://discord.com/channels/1/20/6")
        self.assertEqual(b.count("↗ 摘要"), 1)
        self.assertIn("/20/6", b)

    def test_can_move_only_bot_mirrors(self):
        cfg = N(ch_screenshot=PREVIEW_CH, ch_inbox_mirror=MIRROR_CH)
        dc = N(client=N(user=N(id=1)))
        self.assertTrue(sh.can_move(dc, cfg, Msg(1, MIRROR_CH, "x", author=1)))
        self.assertFalse(sh.can_move(dc, cfg, Msg(1, MIRROR_CH, "x", author=2)))      # 使用者自己發的鏡像
        self.assertFalse(sh.can_move(dc, N(ch_screenshot=None, ch_inbox_mirror=MIRROR_CH), Msg(1, MIRROR_CH, "x")))

    def test_migrate_moves_recent_only(self):
        today = datetime.now(ZoneInfo("Asia/Taipei"))
        d_new, d_old = today.strftime("%Y-%m-%d 10:00"), "2020-01-01 10:00"
        mirror = Msg(100, MIRROR_CH, "# 2026-10-17\nhttps://x.com")
        reply = Msg(101, MIRROR_CH, "**t**\n> a", reference=object())
        chunk2 = Msg(102, MIRROR_CH, "> b")
        stop = Msg(103, MIRROR_CH, "# 2026-10-17\n下一則鏡像")
        old_reply = Msg(55, MIRROR_CH, "舊的", reference=object())
        by_url = {m.jump_url: m for m in (mirror, reply, old_reply)}
        sent = []

        class Ch:
            async def send(self, c, **k):
                m = Msg(900 + len(sent), PREVIEW_CH, c); sent.append(m); return m

        async def fetch(url): return by_url.get(url)
        async def channel(_): return Ch()
        reply.channel.history = lambda **k: _aiter([chunk2, stop])
        dc = N(client=N(user=N(id=1)), fetch_by_url=fetch, channel=channel)
        updates = []
        mirrors = {mirror.jump_url: [], }

        class Index:
            entries = {
                1: N(rec=N(reply=reply.jump_url, date=d_new, mirror=mirror.jump_url, key=1)),
                2: N(rec=N(reply=old_reply.jump_url, date=d_old, mirror="m-old", key=2)),
            }
            def by_mirror(self, url): return [N()] if url == stop.jump_url else []
            async def update(self, key, **kw): updates.append((key, kw))
        cfg = N(ch_screenshot=PREVIEW_CH, ch_inbox_mirror=MIRROR_CH, tz="Asia/Taipei")
        with tempfile.TemporaryDirectory() as tmp, mock.patch("paths.data_dir", lambda: Path(tmp)), \
                mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            n = asyncio.run(sh.migrate_recent(dc, cfg, Index()))
            n2 = asyncio.run(sh.migrate_recent(dc, cfg, Index()))
        self.assertEqual((n, n2), (1, 0))
        self.assertIn("**t**\n> a\n> b", sent[0].content)                  # 兩則接起來搬
        self.assertIn(mirror.jump_url, sent[0].content.split("\n")[0])      # 開頭有回鏡像的連結
        self.assertTrue(reply.deleted and chunk2.deleted and not stop.deleted and not old_reply.deleted)
        self.assertEqual(updates, [(1, {"reply": sent[0].jump_url})])
        self.assertIn(sent[0].jump_url, mirror.content)                      # 鏡像有箭頭


def _aiter(items):
    async def g():
        for i in items:
            yield i
    return g()


if __name__ == "__main__":
    unittest.main()
