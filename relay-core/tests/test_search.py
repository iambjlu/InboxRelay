# relay-core/tests/test_search.py
import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import records  # noqa: E402
from search import engine  # noqa: E402


def e(day, msg, text, typ="text", gist=None, fn=None):
    r = records.KwRecord(tg_chat=-1, tg_msg=msg, origin="live", date=f"{day} 12:{msg:02d}", type=typ,
                         mirror=f"https://discord.com/channels/1/2/{msg}", excerpt=text, gist=gist, filename=fn)
    return SimpleNamespace(rec=r)


ENTRIES = [e("2026-09-30", 1, "中午吃滿漢大餐泡麵"), e("2026-09-30", 2, "編輯蝦皮"),
           e("2026-09-29", 3, "晚餐便當"), e("2026-09-30", 4, "", "image", gist="一碗牛肉麵的照片")]


class TestEngine(unittest.TestCase):
    def test_parse_query(self):
        q = engine.parse_query('{"from":"2026-09-30","to":"2026-09-30","terms":["吃","餐","便當"],"want":"x"}', "9/30吃了什麼")
        self.assertEqual((q.date_from, q.date_to, q.terms), ("2026-09-30", "2026-09-30", ["吃", "餐", "便當"]))
        self.assertEqual(engine.parse_query("亂七八糟", "原問題").terms, ["原問題"])

    def test_collect_range_takes_all_that_day(self):
        q = engine.Query("2026-09-30", "2026-09-30", ["吃", "餐"])
        c = engine.collect(ENTRIES, {"2026-09-30": ("那天吃了泡麵", "https://discord.com/channels/1/9/30")}, q)
        self.assertEqual({x.url[-2:].strip("/") for x in c}, {"1", "2", "4", "30"})   # 9/29 被排除

    def test_collect_small_set_gives_all(self):
        c = engine.collect(ENTRIES, {}, engine.Query(terms=["便當"]))
        self.assertEqual(len(c), 4)          # 資料少：全部給模型判斷，不會因為字面沒命中就找不到

    def test_collect_large_set_needs_hit(self):
        many = [e("2026-09-01", i, f"雜訊{i}") for i in range(1, 60)] * 3 + [e("2026-09-02", 99, "晚餐便當")]
        c = engine.collect(many, {}, engine.Query(terms=["便當"]))
        self.assertEqual([x.excerpt for x in c], ["晚餐便當"])

    def test_excerpt_rules(self):
        self.assertEqual(engine._excerpt(ENTRIES[3].rec, []), "一碗牛肉麵的照片")
        self.assertEqual(engine._excerpt(e("d", 9, "x", "file", gist="報名簡章。第二句", fn="簡章.pdf").rec, []),
                         "簡章.pdf｜報名簡章")

    def test_answer_and_format(self):
        ans, picks = engine.parse_answer("那天中午吃了滿漢大餐 [1]。\n相關：1,9,1", 3)
        self.assertEqual(picks, [1])
        cands = engine.collect(ENTRIES, {}, engine.Query("2026-09-30", "2026-09-30", ["吃"]))
        out = engine.format_reply(ans, cands, [1])
        self.assertIn("📅 2026-09-30 12:01｜💬 文字", out)
        self.assertIn("[↗ 跳到鏡像](<https://discord.com/channels/1/2/1>)", out)
        self.assertNotIn("tg_msg", out)

    def test_full_search_with_fake_llm(self):
        class LLM:
            async def chat(self, prompt, data=None, **kw):
                if "JSON" in prompt:
                    return '{"from":"2026-09-30","to":"2026-09-30","terms":["吃","餐","麵"],"want":"食物"}'
                return "9/30 中午吃了滿漢大餐泡麵 [1]，還有牛肉麵 [3]。\n相關：1,3"
        now = datetime(2026, 10, 2, 9, tzinfo=ZoneInfo("Asia/Taipei"))
        async def source(q):
            return engine.collect(ENTRIES, {}, q)
        reply, q, n = asyncio.run(engine.search(LLM(), "9/30吃了什麼", source, now))
        self.assertEqual(n, 3)
        self.assertIn("滿漢大餐", reply)
        self.assertEqual(reply.count("📅 2026-09-30"), 2)


class TestFeedback(unittest.TestCase):
    def test_route_fix_daily(self):
        from feedback import FeedbackChannel

        class LLM:
            async def chat(self, prompt, data=None, **kw):
                return ('{"action":"fix_daily","date":"2026-09-30","month":"",'
                        '"instruction":"補上滿漢大餐","memory_note":"總結要列出吃的東西","reply":""}')

        calls = {}

        class RC:
            def has(self, k, key):
                return False

            def first(self, k, key):
                return SimpleNamespace(jump_url="https://discord.com/channels/1/9/9")

        class R:
            rc = RC()
            _lock = asyncio.Lock()

            def _entries(self, d):
                return [1]

            async def add_note(self, k, ins):
                calls["note"] = (k, ins)

            async def summarize_day(self, d):
                calls["day"] = d

        class Mem:
            def add_correction(self, t):
                calls["mem"] = t

            async def upgrade(self, reason):
                return True

        replies = []

        class Msg:
            content = "9/30 的每日總結沒有出現滿漢大餐，要補上，以後都要列出吃的東西"

            async def reply(self, t, **kw):
                replies.append(t)

        async def go():
            R._lock = asyncio.Lock()
            fb = FeedbackChannel.__new__(FeedbackChannel)
            fb.cfg, fb.llm, fb.recapper, fb.memory = SimpleNamespace(tz="Asia/Taipei"), LLM(), R(), Mem()
            await fb._route(Msg())
            await asyncio.sleep(0)
        asyncio.run(go())
        self.assertEqual(calls["note"], ("2026-09-30", "補上滿漢大餐"))
        self.assertEqual(calls["day"], "2026-09-30")
        self.assertEqual(calls["mem"], "總結要列出吃的東西")
        self.assertIn("已依你的要求改寫 **2026-09-30**", replies[0])


if __name__ == "__main__":
    unittest.main()


class TestFollowup(unittest.TestCase):
    def _chain(self):
        import discord  # noqa: F401
        me = SimpleNamespace(id=1)
        user = SimpleNamespace(id=2)
        q1 = SimpleNamespace(id=10, author=user, content="有個後端服務忘記名字了", reference=None)
        a1 = SimpleNamespace(id=11, author=me, content="可能是 Supabase [↗1](<https://x/1>)\n-# 搜尋：AI 測試頻道",
                             reference=SimpleNamespace(message_id=10, resolved=q1))
        q2 = SimpleNamespace(id=12, author=user, content="它是什麼", reference=SimpleNamespace(message_id=11, resolved=a1))
        return me, q2

    def test_conversation_from_reply_chain(self):
        from search.channel import SearchChannel
        me, q2 = self._chain()
        sc = SearchChannel.__new__(SearchChannel)
        sc.dc = SimpleNamespace(client=SimpleNamespace(user=me))
        import search.channel as ch
        orig = ch.discord.Message
        ch.discord.Message = SimpleNamespace     # 讓假物件通過 isinstance 檢查
        try:
            turns = asyncio.run(sc._conversation(q2))
        finally:
            ch.discord.Message = orig
        self.assertEqual(turns, [("使用者", "有個後端服務忘記名字了"), ("助手", "可能是 Supabase ↗1")])

    def test_new_message_is_not_followup(self):
        from search.channel import SearchChannel
        sc = SearchChannel.__new__(SearchChannel)
        sc.dc = SimpleNamespace(client=SimpleNamespace(user=SimpleNamespace(id=1)))
        msg = SimpleNamespace(reference=None)
        self.assertEqual(asyncio.run(sc._conversation(msg)), [])
