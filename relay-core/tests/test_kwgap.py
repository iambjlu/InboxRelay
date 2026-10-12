# relay-core/tests/test_kwgap.py：#關鍵字庫 編輯間隔的自動調整（只認自己頻道的 429、一次編輯加碼一次、很快恢復）
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kwindex  # noqa: E402
from kwindex import _Gap, _RateWatch  # noqa: E402


def warn(channel_id, method="PATCH"):
    return logging.LogRecord("discord.http", logging.WARNING, "", 0,
                             f"We are being rate limited. {method} https://discord.com/api/v10/channels/{channel_id}/messages/9 "
                             "responded with 429. Retrying in 4.90 seconds.", None, None)


class GapTest(unittest.TestCase):
    def setUp(self):
        _Gap.value, _Gap.at, _Gap.bumped, _Gap.channels = 0.0, 0.0, False, {111}
        self.h = _RateWatch()

    def test_only_own_channel_and_patch_count(self):
        self.h.emit(warn(222))                                   # 別的頻道（例如鏡像小箭頭）不理
        self.h.emit(warn(111, "POST"))                           # 不是編輯
        self.assertEqual(_Gap.value, 0.0)
        with mock.patch.object(kwindex.time, "monotonic", lambda: 100.0):
            self.h.emit(warn(111))
            self.assertAlmostEqual(_Gap.current(), 1.8)           # 基準 1.0 × 1.5 + 0.3
        self.assertTrue(_Gap.bumped)

    def test_one_bump_per_edit_and_cap(self):
        with mock.patch.object(kwindex.time, "monotonic", lambda: 100.0):
            for _ in range(5):                                    # 同一次編輯 discord.py 重試 5 次
                self.h.emit(warn(111))
            first = _Gap.current()
            self.assertAlmostEqual(first, 1.8)                    # 只加碼一次，不是連乘 5 次
            for _ in range(10):                                   # 連續 10 次不同的編輯都被限流 → 到上限為止
                _Gap.bumped = False
                self.h.emit(warn(111))
            self.assertAlmostEqual(_Gap.current(), kwindex.MAX_GAP_SEC)

    def test_recovers_quickly_with_time(self):
        now = {"t": 100.0}
        with mock.patch.object(kwindex.time, "monotonic", lambda: now["t"]):
            for _ in range(10):
                _Gap.bumped = False
                self.h.emit(warn(111))
            self.assertAlmostEqual(_Gap.current(), 4.0)
            now["t"] += 20                                        # 一個半衰期
            self.assertAlmostEqual(_Gap.current(), 2.0, places=1)
            now["t"] += 60
            self.assertEqual(_Gap.current(), kwindex.EDIT_GAP_SEC)   # 回到基準


if __name__ == "__main__":
    unittest.main()
