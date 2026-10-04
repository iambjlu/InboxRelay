# relay-core/tests/test_kwx.py
import asyncio
import itertools
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as N

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import records  # noqa: E402
from kwindex import KwIndex  # noqa: E402

BOT = 1


class FakeChannel:
    def __init__(self):
        self.msgs = {}                 # id → content
        self.ids = itertools.count(100)

    async def send(self, content):
        i = next(self.ids)
        self.msgs[i] = content
        return N(id=i, content=content, author=N(id=BOT))

    def get_partial_message(self, i):
        ch = self

        class P:
            async def edit(self, content):
                ch.msgs[i] = content

            async def delete(self):
                ch.msgs.pop(i, None)
        return P()

    async def history(self, limit=None, oldest_first=True, after=None):
        for i in sorted(self.msgs):
            yield N(id=i, content=self.msgs[i], author=N(id=BOT))


class OcrMessageTest(unittest.TestCase):
    def test_one_message_per_image(self):
        async def go():
            ch = FakeChannel()
            idx = KwIndex(ch, BOT)
            rec = records.KwRecord(-1, 7, "live", "2026-10-01 10:00", "image", "u", filename="a.jpg")
            idx.add(rec, await ch.send(records.format_kw(rec)))
            await idx.update(rec.key, ocr="全聯 福利中心\n小計 320 元")
            self.assertEqual(len(ch.msgs), 2)                           # 主紀錄＋這張圖的 OCR 訊息
            self.assertTrue(any(c.startswith("OCR|-1|7\n全聯 福利中心 小計 320 元") for c in ch.msgs.values()))
            idx2 = KwIndex(ch, BOT)                                     # 重新載入 → 文字接回紀錄
            await idx2.load()
            self.assertEqual(idx2.get(rec.key).rec.ocr, "全聯 福利中心 小計 320 元")
            await idx2.update(rec.key, kw="全聯")                        # 其他欄位更新不動 OCR 訊息
            self.assertEqual(len(ch.msgs), 2)
            await idx2.update(rec.key, ocr="（無文字）")                  # 沒字 → OCR 訊息刪掉
            self.assertEqual(len(ch.msgs), 1)
            await idx2.update(rec.key, ocr="再一次")
            await idx2.delete(rec.key)
            self.assertEqual(ch.msgs, {})
        asyncio.run(go())

    def test_legacy_inline_moves_to_message(self):
        async def go():
            ch = FakeChannel()
            await ch.send("KW|v2\ntg_chat: -1\ntg_msg: 8\norigin: live\ndate: 2026-10-01 10:00\ntype: image\n"
                          "mirror: u\nfilename: b.jpg\nocr: 舊的 ⏎ 文字\nenriched: yes")
            idx = KwIndex(ch, BOT)
            await idx.load()
            e = idx.get((-1, 8))
            self.assertTrue(e.legacy)
            await idx.update((-1, 8))
            self.assertTrue(any("ocr: 見 OCR 訊息" in c for c in ch.msgs.values()))
            self.assertTrue(any(c == "OCR|-1|8\n舊的 文字" for c in ch.msgs.values()))
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
