# relay-core/tests/test_search_think.py：思考內容不外流、搜尋候選更完整（測試資料都是自己編的）
import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm import strip_reasoning  # noqa: E402
from search import engine  # noqa: E402
from search.engine import Candidate, Query, expand_terms, has_time_hint  # noqa: E402


class StripReasoningTest(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(strip_reasoning("<think>內部想法</think>最終答案"), "最終答案")
        self.assertEqual(strip_reasoning("內部想法\n</think>\n最終答案"), "最終答案")            # 開頭標籤被樣板吃掉
        self.assertEqual(strip_reasoning("<think>想到一半被截斷"), "")                          # 沒有答案
        self.assertEqual(strip_reasoning("Here's a thinking process:\n1. 分析\n2. 草稿…"), "")  # 思考寫在正文、沒寫完
        self.assertEqual(strip_reasoning("  一般的回答  "), "一般的回答")
        self.assertEqual(strip_reasoning(""), "")


class QueryTest(unittest.TestCase):
    def test_time_hint(self):
        self.assertFalse(has_time_hint("我有傳過菜單或餐廳之類的嗎"))
        for q in ("昨天吃了什麼", "10/2 的消費", "去年夏天的旅遊", "2022/3/3 那天", "上個月的發票", "最近的餐廳"):
            self.assertTrue(has_time_hint(q), q)

    def test_expand_terms_adds_food_words_only_for_food_questions(self):
        t = expand_terms("晚餐要吃什麼", ["晚餐", "菜單"])
        self.assertTrue({"吃", "飯", "麵", "火鍋", "便當"} <= set(t))
        self.assertEqual(t[:2], ["晚餐", "菜單"])
        self.assertEqual(expand_terms("後端服務叫什麼", ["Supabase", "Firebase"]), ["Supabase", "Firebase"])
        self.assertLessEqual(len(expand_terms("吃", [f"w{i}" for i in range(40)])), 45)


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def chat(self, prompt, data=None, max_tokens=800, thinking=False, **k):
        self.calls.append((prompt[:6], thinking, max_tokens))
        if prompt.startswith("今天是"):      # SEARCH_PARSE
            return '{"from": "2026-01-01", "to": "2026-10-09", "terms": ["晚餐", "菜單"], "want": "x"}'
        return strip_reasoning(self.answers.pop(0))              # 真正的 LLM.chat 回傳前就會做


def cands(n=3):
    return [Candidate(f"2026-0{i + 1}-0{i + 1} 12:00", "text", f"內容{i}", f"節錄{i}", f"https://discord.com/channels/1/2/{i}")
            for i in range(n)]


NOW = datetime(2026, 10, 9, 21, 0, tzinfo=ZoneInfo("Asia/Taipei"))


class SearchFlowTest(unittest.TestCase):
    def _run(self, llm, question="我有傳過菜單或餐廳之類的嗎", thinking=True):
        seen = {}

        async def source(q: Query):
            seen["q"] = q
            return cands()
        out = asyncio.run(engine.search(llm, question, source, NOW, thinking=thinking))
        return out, seen["q"]

    def test_invented_date_range_dropped_and_terms_expanded(self):
        (reply, q, n), q2 = self._run(FakeLLM(["- 店名 [1]\n相關：1"]))
        self.assertEqual((q.date_from, q.date_to), ("", ""))                    # 問題沒提時間 → 不限日期
        self.assertIn("火鍋", q.terms)                                           # 補上這一類的常見詞
        self.assertIn("店名", reply)

    def test_explicit_date_is_kept(self):
        (reply, q, n), _ = self._run(FakeLLM(["好 [1]\n相關：1"]), question="昨天吃了什麼")
        self.assertEqual(q.date_from, "2026-01-01")                              # 有提時間：照模型解析的

    def test_truncated_thinking_retries_without_thinking(self):
        llm = FakeLLM(["Here's a thinking process:\n1. 分析…", "- 重試後的答案 [2]\n相關：2"])
        (reply, q, n), _ = self._run(llm)
        self.assertNotIn("thinking process", reply)
        self.assertIn("重試後的答案", reply)
        self.assertEqual([c[1] for c in llm.calls[1:]], [True, False])           # 第一次思考、第二次關掉思考
        self.assertEqual(llm.calls[1][2], 8000)                                  # 思考時給足 token

    def test_both_empty_gives_friendly_message(self):
        (reply, q, n), _ = self._run(FakeLLM(["", ""]))
        self.assertIn("沒有完整回答", reply)
        self.assertNotIn("thinking", reply.lower())


if __name__ == "__main__":
    unittest.main()
