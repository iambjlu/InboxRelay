# relay-core/tests/test_records_album.py
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import formatting as f  # noqa: E402
import records as r  # noqa: E402
from album import AlbumBuffer  # noqa: E402


class TestRecords(unittest.TestCase):
    def rec(self, **kw):
        base = dict(tg_chat=-370080868, tg_msg=12, origin="live", date="2026-10-02 14:22",
                    type="image", mirror="https://discord.com/channels/1/2/3")
        base.update(kw)
        return r.KwRecord(**base)

    def test_roundtrip(self):
        text = r.format_kw(self.rec(filename="a b.jpg", excerpt="第一行\n第二行"))
        self.assertTrue(text.startswith("KW|v2\n"))
        self.assertIn("excerpt: 第一行 第二行", text)
        self.assertTrue(text.endswith("enriched: no"))
        self.assertNotIn("src:", text)  # 一般群組沒有 src
        self.assertEqual(r.parse_key(text), (-370080868, 12))
        self.assertEqual(r.parse_kw(text)["filename"], "a b.jpg")

    def test_long_excerpt_fits(self):
        text = r.format_kw(self.rec(excerpt="字" * 5000))
        self.assertLessEqual(len(text), r.MAX_LEN)
        self.assertTrue(text.endswith("enriched: no"))

    def test_not_kw(self):
        self.assertIsNone(r.parse_key("# 2026-10-02\nhello"))


class TestMirrorContent(unittest.TestCase):
    def test_attachment_only_first_of_day(self):
        self.assertEqual(f.build_mirror_content("2026-10-02", ""), "# 2026-10-02")

    def test_attachment_only_not_first(self):
        self.assertEqual(f.build_mirror_content("2026-10-02", "", with_header=False), "")

    def test_oversize(self):
        m = f.build_mirror_content("2026-10-02", "看這個", ["big.zip"], with_header=False)
        self.assertEqual(m, "看這個\n📎 big.zip（過大未搬移）")

    def test_limit_with_everything(self):
        m = f.build_mirror_content("2026-10-02", "字" * 5000, ["x.zip"] * 3, "https://t.me/c/1/2")
        self.assertLessEqual(len(m), f.DISCORD_LIMIT)
        self.assertTrue(m.endswith("-# [src](<https://t.me/c/1/2>)"))
        self.assertIn("📎 x.zip（過大未搬移）", m)


class TestAlbum(unittest.TestCase):
    def test_album_merge_and_order(self):
        out = []

        async def emit(pieces):
            out.append(list(pieces))

        async def scenario():
            buf = AlbumBuffer(emit, delay=0.05, sort_key=lambda x: x)
            await buf.add("g1", 3)
            await buf.add("g1", 2)
            await buf.add("g1", 4)
            await buf.add(None, 5)       # 別的訊息 → 先送出相簿
            await buf.add("g2", 6)
            await buf.add("g2", 7)
            await asyncio.sleep(0.15)    # 計時器送出 g2

        asyncio.run(scenario())
        self.assertEqual(out, [[2, 3, 4], [5], [6, 7]])


if __name__ == "__main__":
    unittest.main()
