# relay-core/tests/test_search_more.py：搜尋依關聯度排序、分批、「顯示更多」按鈕（測試資料都是自己編的）
import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as N
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm import strip_reasoning  # noqa: E402
from search import channel, engine  # noqa: E402
from search.engine import Candidate, Query  # noqa: E402

NOW = datetime(2026, 10, 9, 21, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def cand(i, score, day=None):
    return Candidate(day or f"2026-0{(i % 9) + 1}-0{(i % 9) + 1} 12:00", "text", f"內容{i}", f"節錄{i}",
                     f"https://discord.com/channels/1/2/{i}", score)


class RankTest(unittest.TestCase):
    def test_score_weights_core_terms_higher(self):
        self.assertEqual(engine.score_text("有火鍋", ["火鍋"], ["火鍋"]), 2.0)
        self.assertEqual(engine.score_text("有火鍋", ["火鍋"]), 1.0)
        self.assertEqual(engine.score_text("火鍋 火鍋", ["火鍋"], []), 1.2)

    def test_rank_orders_by_score_and_keeps_all_hits(self):
        n = engine.MAX_CANDIDATES + 40
        cs = [cand(i, float(i % 7)) for i in range(n)]
        r = engine.rank(cs, Query())
        self.assertTrue(all(r[i].score >= r[i + 1].score for i in range(len(r) - 1)))
        self.assertEqual(len(r), len([c for c in cs if c.score > 0]))                # 沒有被截斷成一批
        self.assertEqual(len(engine.rank([cand(1, 0), cand(2, 0)], Query())), 2)       # 資料少：沒命中也留著
        first = engine.page(r, 0)
        self.assertEqual(len(first), engine.MAX_CANDIDATES)
        self.assertEqual(first, sorted(first, key=lambda c: c.date))                  # 給模型的一批依時間排
        self.assertTrue(min(c.score for c in first) >= max(c.score for c in engine.page(r, engine.MAX_CANDIDATES)) - 1e-9)

    def test_search_ranked_returns_rest(self):
        class LLM:
            async def chat(self, prompt, data=None, **k):
                if prompt.startswith("今天是"):
                    return '{"from":"","to":"","terms":["吃"],"want":"x"}'
                return strip_reasoning("- 一個答案 [1]\n相關：1")
        n = engine.MAX_CANDIDATES + 30
        cs = [cand(i, 1.0 + i / 1000) for i in range(n)]

        async def source(q):
            return engine.rank(cs, q)
        r = asyncio.run(engine.search_ranked(LLM(), "吃", source, NOW))
        self.assertEqual((r.total, r.shown, len(r.rest)), (n, engine.MAX_CANDIDATES, 30))
        self.assertIn("一個答案", r.reply)
        self.assertTrue(all(c.score <= min(x.score for x in engine.page(engine.rank(cs, Query()), 0)) for c in r.rest))


class MoreButtonTest(unittest.TestCase):
    def test_custom_id_roundtrip(self):
        import re
        m = re.fullmatch(channel.MoreButton.__discord_ui_compiled_template__.pattern, channel.MORE_PREFIX + "0a1b2c3d")
        self.assertEqual(m["token"], "0a1b2c3d")

    def test_on_more_posts_next_batch_and_new_button(self):
        async def go():
            sent, edits, resp = [], [], []

            class Msg:
                async def edit(self, view=None):
                    edits.append(view)

                async def reply(self, c, mention_author=False, suppress_embeds=True, view=None):
                    sent.append((c, view))
                    return N(channel=None)
            dc = N(add_message_handler=lambda h: None, client=N(add_dynamic_items=lambda *a: None, user=N(id=1)))

            class LLM:
                async def chat(self, prompt, data=None, **k):
                    return strip_reasoning("- 第二批答案 [1]\n相關：1")
            ch = channel.SearchChannel(N(tz="Asia/Taipei"), dc, LLM(), None, None, set(), set())
            rest = [cand(i, 1.0) for i in range(engine.MAX_CANDIDATES + 5)]
            token = ch._remember_more(channel.MoreState("吃什麼", Query(terms=["吃"]), rest, 150, 305 + 150, "投遞箱"))

            class Inter:
                message = Msg()
                response = N(defer=lambda: asyncio.sleep(0),
                             send_message=lambda text, ephemeral=False: resp.append(text) or asyncio.sleep(0))
                followup = N(send=lambda *a, **k: asyncio.sleep(0))
            await ch.on_more(Inter(), token)
            self.assertEqual(edits, [None])                                              # 用掉的按鈕拿掉
            self.assertIn("第二批答案", sent[0][0])
            self.assertIn("關聯度第 151～300 名", sent[0][0])
            self.assertIsNotNone(sent[0][1])                                              # 還有剩 → 新的按鈕
            self.assertEqual(len(ch._more[token].rest), 5)
            await ch.on_more(Inter(), token)                                              # 最後一批
            self.assertIn("已經是最後一批", sent[1][0])
            self.assertIsNone(sent[1][1])
            self.assertNotIn(token, ch._more)
            await ch.on_more(Inter(), token)                                              # 資料已不在
            self.assertIn("已經不在了", resp[-1])
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
