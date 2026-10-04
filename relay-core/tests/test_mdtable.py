# relay-core/tests/test_mdtable.py：Discord 不支援表格 → 模型回的表格轉成條列；每次呼叫模型都帶「不要表格」規則
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import prompts  # noqa: E402
from llm import LLM  # noqa: E402
from mdtable import tables_to_bullets as t2b  # noqa: E402


class TableTest(unittest.TestCase):
    def test_three_columns(self):
        src = "比較如下：\n| 項目 | 價格 | 備註 |\n|------|:----:|------|\n| 蘋果 | 30 | 便宜 |\n| **香蕉** | 20 |  |\n之後"
        self.assertEqual(t2b(src), "比較如下：\n- 蘋果：價格 30；備註 便宜\n- 香蕉：價格 20\n之後")

    def test_two_columns_and_no_outer_pipes(self):
        src = "名稱 | 說明\n--- | ---\nA | 第一個\nB | 第二個"
        self.assertEqual(t2b(src), "- A：第一個\n- B：第二個")

    def test_code_block_and_plain_pipes_untouched(self):
        code = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
        self.assertEqual(t2b(code), code)
        plain = "用 a | b 表示或，下一行也有 | 但不是表格\n---"
        self.assertEqual(t2b(plain), plain)
        self.assertEqual(t2b(""), "")
        self.assertEqual(t2b("沒有表格"), "沒有表格")

    def test_escaped_pipe_and_ragged_rows(self):
        src = "| 指令 | 說明 | 範例 |\n|---|---|---|\n| a\\|b | 或 |\n| c | 短 | x |"
        self.assertEqual(t2b(src), "- a|b：說明 或\n- c：說明 短；範例 x")

    def test_system_prompt_has_format_rule(self):
        llm = LLM("http://x/v1", "m", "k")
        self.assertIn("不要輸出表格", llm._system(False))
        self.assertIn("不要輸出表格", llm._system(True))
        self.assertIn("條列", prompts.FORMAT_RULE)

    def test_chat_system_prompt_has_format_rule(self):
        from types import SimpleNamespace as N
        from chat import ChatChannel
        c = ChatChannel(N(ch_chat=1, ch_chat_prompt=2, tz="Asia/Taipei"), N(), None, set())
        self.assertIn(prompts.FORMAT_RULE, c.system_prompt())


if __name__ == "__main__":
    unittest.main()
