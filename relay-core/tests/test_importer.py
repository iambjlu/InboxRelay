# relay-core/tests/test_importer.py
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import formatting as f  # noqa: E402
from importer.packer import pack_all, attachment_content  # noqa: E402
from importer.reader import iter_messages, read_meta, to_bot_chat_id  # noqa: E402

TZ = "Asia/Taipei"


def ts(day: int, hour: int = 12) -> str:
    # 2021-03-day hour:00 台北 → unixtime
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return str(int(datetime(2021, 3, day, hour, tzinfo=ZoneInfo(TZ)).timestamp()))


SAMPLE = {
    "name": "投遞箱", "type": "private_group", "id": 370080868,
    "messages": [
        {"id": 1, "type": "service", "date_unixtime": ts(5), "action": "create_group", "text": ""},
        {"id": 2, "type": "message", "date_unixtime": ts(5, 9), "text": "早安",
         "text_entities": [{"type": "plain", "text": "早安"}]},
        {"id": 3, "type": "message", "date_unixtime": ts(5, 10),
         "text": ["看 ", {"type": "link", "text": "https://x.com"}],
         "text_entities": [{"type": "plain", "text": "看 "}, {"type": "link", "text": "https://x.com"}]},
        {"id": 4, "type": "message", "date_unixtime": ts(5, 11), "photo": "photos/photo_1.jpg",
         "text": "截圖", "text_entities": [{"type": "plain", "text": "截圖"}]},
        {"id": 5, "type": "message", "date_unixtime": ts(5, 12), "text": "午安", "text_entities": []},
        {"id": 6, "type": "message", "date_unixtime": ts(6, 9),
         "file": "(File exceeds maximum size. Change data exporting settings to download.)",
         "file_name": "大檔.zip", "text": ""},
        {"id": 7, "type": "message", "date_unixtime": ts(6, 10), "media_type": "sticker",
         "file": "stickers/s.webp", "sticker_emoji": "😀", "text": ""},
        {"id": 8, "type": "message", "date_unixtime": ts(6, 11), "text": "", "text_entities": []},
    ],
}


class TestReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "result.json"
        self.path.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_meta(self):
        m = read_meta(self.path)
        self.assertEqual(m.bot_chat_id, -370080868)
        self.assertEqual(to_bot_chat_id("private_supergroup", 123), -100123)

    def test_messages(self):
        ms = list(iter_messages(self.path, TZ))
        self.assertEqual([m.tg_msg for m in ms], [2, 3, 4, 5, 6, 7])  # 服務訊息與空訊息略過
        self.assertEqual(ms[1].type, "link")
        self.assertEqual(ms[1].text, "看 https://x.com")
        self.assertEqual((ms[2].type, ms[2].rel_path, ms[2].file_name), ("image", "photos/photo_1.jpg", "photo_1.jpg"))
        self.assertEqual((ms[4].missing_reason, ms[4].file_name), ("過大未匯出", "大檔.zip"))
        self.assertEqual((ms[5].type, ms[5].text), ("sticker", "[貼圖 😀]"))

    def test_packing(self):
        pk = list(pack_all(iter_messages(self.path, TZ), TZ))
        # 3/5：[早安, 看…] → 照片單獨 → [午安]；3/6：大檔(未匯出) → [貼圖]
        self.assertEqual([[m.tg_msg for m in p.msgs] for p in pk], [[2, 3], [4], [5], [6], [7]])
        self.assertEqual(pk[0].content, "# 2021-03-05-Friday\n早安\n看 https://x.com")
        self.assertFalse(pk[1].has_header)
        self.assertEqual(attachment_content(pk[1], None), "截圖")
        self.assertEqual(pk[2].content, "午安")  # 同一天不重複標日期
        self.assertTrue(pk[3].has_header)
        self.assertEqual(attachment_content(pk[3], "📎 大檔.zip（過大未匯出）"),
                         "# 2021-03-06-Saturday\n📎 大檔.zip（過大未匯出）")
        self.assertEqual(pk[4].content, "[貼圖 😀]")

    def test_continue_from_existing_header(self):
        pk = list(pack_all(iter_messages(self.path, TZ), TZ, last_header_date="2021-03-05"))
        self.assertEqual(pk[0].content, "早安\n看 https://x.com")


class TestPackerLimits(unittest.TestCase):
    def _msgs(self, texts, day=5):
        from importer.reader import HistMsg
        from datetime import datetime, timezone
        return [HistMsg(i + 1, datetime(2021, 3, day, 4, tzinfo=timezone.utc), t, "text")
                for i, t in enumerate(texts)]

    def test_split_without_repeating_header(self):
        pk = list(pack_all(self._msgs(["a" * 900] * 5), TZ))
        self.assertGreater(len(pk), 1)
        self.assertTrue(pk[0].content.startswith("# 2021-03-05-Friday\n"))
        for p in pk:
            self.assertLessEqual(len(p.content), f.DISCORD_LIMIT)
        for p in pk[1:]:
            self.assertFalse(p.content.startswith("#"))
        self.assertEqual(sum(len(p.msgs) for p in pk), 5)

    def test_single_long_message(self):
        pk = list(pack_all(self._msgs(["短", "長" * 5000, "尾"]), TZ))
        self.assertEqual([[m.tg_msg for m in p.msgs] for p in pk], [[1], [2], [3]])
        self.assertIn(f.TRUNC_SUFFIX, pk[1].content)
        self.assertLessEqual(len(pk[1].content), f.DISCORD_LIMIT)


if __name__ == "__main__":
    unittest.main()
