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
