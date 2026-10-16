# relay-core/tests/test_reverse.py
import asyncio
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dc_reverse  # noqa: E402
from dc_reverse import ReverseSync, is_photo, split_text  # noqa: E402


class Att:
    def __init__(self, name, size=1000, ct="image/png"):
        self.filename, self.size, self.content_type = name, size, ct

    async def read(self):
        return b"x" * 10


class Msg:
    _n = 100

    def __init__(self, photo=False, doc=False):
        Msg._n += 1
        self.chat_id, self.message_id = -1, Msg._n
        self.photo = [types.SimpleNamespace(file_unique_id=f"p{self.message_id}")] if photo else []
        self.document = types.SimpleNamespace(file_unique_id=f"d{self.message_id}") if doc else None
        self.video = self.audio = self.animation = None


class Bot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat, text, **kw):
        self.calls.append(("text", text)); return Msg()

    async def send_photo(self, chat, b, caption=None, filename=None):
        self.calls.append(("photo", caption)); return Msg(photo=True)

    async def send_document(self, chat, b, caption=None, filename=None):
        self.calls.append(("doc", caption)); return Msg(doc=True)

    async def send_media_group(self, chat, media):
        self.calls.append(("group", len(media))); return [Msg(photo=True) for _ in media]


class ReverseTest(unittest.TestCase):
    def setUp(self):
        self.bot = Bot()
        cfg = types.SimpleNamespace(tg_inbox_chat_id=-1, reverse_sync=True)
        self.rs = ReverseSync(types.SimpleNamespace(bot=self.bot, cfg=cfg))

    def test_helpers(self):
        self.assertTrue(is_photo(Att("a.jpg", ct="image/jpeg")))
        self.assertFalse(is_photo(Att("a.gif", ct="image/gif")))
        self.assertFalse(is_photo(Att("a.png", size=11 * 1024 * 1024)))
        self.assertEqual(len(split_text("a" * 5000)), 2)

    def test_text_only(self):
        out = asyncio.run(self.rs._send("編輯報價單", []))
        self.assertEqual(self.bot.calls, [("text", "編輯報價單")])
        self.assertEqual(out[0][1], "text")

    def test_caption_on_first_photo_then_docs(self):
        atts = [Att("a.png"), Att("b.jpg", ct="image/jpeg"), Att("c.pdf", ct="application/pdf")]
        out = asyncio.run(self.rs._send("看這個", atts))
        self.assertEqual(self.bot.calls, [("group", 2), ("doc", None)])
        self.assertEqual([o[3] for o in out], ["看這個", None, None])
        self.assertEqual([o[2] for o in out], ["a.png", "b.jpg", "c.pdf"])

    def test_long_text_separate_and_big_file_noted(self):
        atts = [Att("huge.zip", size=60 * 1024 * 1024, ct="application/zip")]
        asyncio.run(self.rs._send("x" * 2000, atts))
        self.assertEqual(self.bot.calls[0][0], "text")
        self.assertIn("檔案太大", self.bot.calls[0][1])
        self.assertEqual(len(self.bot.calls), 1)


if __name__ == "__main__":
    unittest.main()


class ForwardBackfillTest(unittest.TestCase):
    def test_forward_relay_and_backfill(self):
        import discord
        import tempfile
        from unittest import mock
        N = types.SimpleNamespace
        bot = Bot()
        cfg = types.SimpleNamespace(tg_inbox_chat_id=-1, reverse_sync=True, ch_inbox_mirror=7, tz="Asia/Taipei")
        sent_rel = []

        class Lock:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        def fwd(i, content="# 2026-10-12", atts=(), done=False):
            return N(id=i, channel=N(id=7), author=N(bot=False), webhook_id=None, type=discord.MessageType.default,
                     clean_content="", attachments=[], jump_url=f"u{i}", message_snapshots=[N(content=content, attachments=list(atts))])
        plain = N(id=1, channel=N(id=7), author=N(bot=False), webhook_id=None, type=discord.MessageType.default,
                  clean_content="hi", attachments=[], jump_url="u1", message_snapshots=[])
        old = fwd(2, atts=[Att("a.png")])
        done = fwd(3)
        text_only = fwd(4, atts=[])

        class Hist:
            def __init__(self, ms): self.ms = ms
            def __aiter__(self):
                async def g():
                    for m in self.ms: yield m
                return g()

        idx = N(by_mirror=lambda u: [N(rec=N(origin="dc"))] if u == "u3" else [])
        r = N(bot=bot, cfg=cfg, index=idx, lock=Lock(), dc=N(channel=None))

        async def channel(_):
            return N(history=lambda **k: Hist([plain, old, done, text_only]))
        r.dc.channel = channel
        rs = ReverseSync(r)

        async def rec(m, sent): sent_rel.append((m.id, len(sent)))
        rs._record = rec
        with tempfile.TemporaryDirectory() as tmp, mock.patch("paths.data_dir", lambda: Path(tmp)), \
                mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            asyncio.run(rs.backfill_forwards())
            asyncio.run(rs.backfill_forwards())                     # 旗標：只做一次
        self.assertEqual([c[0] for c in bot.calls], ["photo", "text"])
        self.assertEqual(bot.calls[0][1], "# 2026-10-12")           # 轉發的文字當第一張圖的說明
        self.assertEqual([i for i, _ in sent_rel], [2, 4])          # 一般訊息、已同步過的略過


class SweepTest(unittest.TestCase):
    def test_sweep_deletes_discord_when_telegram_deleted(self):
        import time
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from unittest import mock
        N = types.SimpleNamespace
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        date = now.strftime("%Y-%m-%d %H:%M")
        mk = lambda key, origin="dc", mirror="M1": N(rec=N(origin=origin, mirror=mirror, date=date, key=key))
        es = {(-1, 1): mk((-1, 1)), (-1, 2): mk((-1, 2), mirror="M2"), (-1, 3): mk((-1, 3), origin="live", mirror="M3")}
        probed, deleted = [], []

        class Prober:
            async def probe(self, key):
                probed.append(key); return "deleted" if key == (-1, 1) else "alive"

        async def on_deleted(keys, mirror, all_deleted): deleted.append((keys, mirror, all_deleted))
        cfg = types.SimpleNamespace(tz="Asia/Taipei", reverse_sync=True)

        async def go():
            pr = Prober()
            pr._sem = asyncio.Semaphore(1)
            rs = ReverseSync(N(cfg=cfg, prober=pr, index=N(entries=es), on_deleted=on_deleted))
            with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
                a = await rs.sweep_once()
                b = await rs.sweep_once()                         # 間隔沒到 → 不再探測
            return a, b
        a, b = asyncio.run(go())
        self.assertEqual((a, b), (1, 0))
        self.assertEqual(sorted(probed), [(-1, 1), (-1, 2)])      # live 的不在這裡處理
        self.assertEqual(deleted, [([(-1, 1)], "M1", True)])
