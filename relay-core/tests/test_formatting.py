# relay-core/tests/test_formatting.py
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import formatting as f  # noqa: E402


class TestFormatting(unittest.TestCase):
    def test_header_has_space(self):
        self.assertEqual(f.header("2026-10-01"), "# 2026-10-01-Thursday")
        self.assertEqual(f.header("2026-10-01", "small"), "-# 2026-10-01-Thursday")

    def test_local_date_crosses_midnight(self):
        # UTC 10/01 17:00 = 台北 10/02 01:00
        dt = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
        self.assertEqual(f.local_date(dt, "Asia/Taipei"), "2026-10-02")

    def test_normal_group_has_no_src(self):
        self.assertIsNone(f.supergroup_src_url(-370080868, 5))

    def test_supergroup_src(self):
        self.assertEqual(f.supergroup_src_url(-1001234567890, 42), "https://t.me/c/1234567890/42")

    def test_basic_message(self):
        self.assertEqual(f.build_live_message("2026-10-01", "hi"), "# 2026-10-01-Thursday\nhi")

    def test_message_with_src(self):
        m = f.build_live_message("2026-10-01", "hi", "https://t.me/c/1/2")
        self.assertTrue(m.endswith("\n-# [src](<https://t.me/c/1/2>)"))

    def test_truncation_limit(self):
        for src in (None, "https://t.me/c/1234567890/99999"):
            for style in ("large", "small"):
                m = f.build_live_message("2026-10-01", "字" * 5000, src, style)
                self.assertLessEqual(len(m), f.DISCORD_LIMIT)
                self.assertIn(f.TRUNC_SUFFIX, m)

    def test_exact_fit_not_truncated(self):
        body = "a" * (f.DISCORD_LIMIT - len("# 2026-10-01-Thursday\n"))
        m = f.build_live_message("2026-10-01", body)
        self.assertEqual(len(m), f.DISCORD_LIMIT)
        self.assertNotIn(f.TRUNC_SUFFIX, m)

    def test_without_header(self):
        self.assertEqual(f.build_live_message("2026-10-01", "hi", with_header=False), "hi")
        m = f.build_live_message("2026-10-01", "hi", "https://t.me/c/1/2", with_header=False)
        self.assertEqual(m, "hi\n-# [src](<https://t.me/c/1/2>)")

    def test_without_header_truncation(self):
        m = f.build_live_message("2026-10-01", "字" * 5000, with_header=False)
        self.assertEqual(len(m), f.DISCORD_LIMIT)

    def test_parse_header_date(self):
        self.assertEqual(f.parse_header_date("# 2026-10-02\nhi"), "2026-10-02")
        self.assertEqual(f.parse_header_date("-# 2026-10-02\nhi"), "2026-10-02")
        self.assertIsNone(f.parse_header_date("hi\n# 2026-10-02"))
        self.assertEqual(f.parse_header_date("# 2026-10-02-Friday\nhi"), "2026-10-02")   # 新格式
        self.assertEqual(f.display_date("2026-10-02"), "2026-10-02-Friday")
        self.assertIsNone(f.parse_header_date("# 今天好熱"))


if __name__ == "__main__":
    unittest.main()
