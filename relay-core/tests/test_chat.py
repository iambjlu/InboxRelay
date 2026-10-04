# relay-core/tests/test_chat.py
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as N

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import discord  # noqa: E402

import chat  # noqa: E402
from chat import ChatChannel, merge_roles, thread_title, trim_history  # noqa: E402

BOT, ME = 1, 2


class Att:
    def __init__(self, i, name, ct, data=b"abc", size=3):
        self.id, self.filename, self.content_type, self.size, self._d = i, name, ct, size, data

    async def read(self):
        return self._d


def msg(i, author, text, atts=()):
    return N(id=i, author=N(id=author, bot=author == BOT), content=text, clean_content=text,
             attachments=list(atts), type=discord.MessageType.default)


class FakeLLM:
    image_support = True

    def __init__(self):
        self.got = None

    async def chat_messages(self, messages, max_tokens=4000, thinking=False, progress=None):
        self.got = (messages, thinking)
        return "好的", {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020}


class ChatTest(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(thread_title("/思考  幫我  看這段"), "幫我 看這段")
        self.assertEqual(len(thread_title("字" * 100)), 41)
        m = merge_roles([{"role": "user", "content": "a"}, {"role": "user", "content": "b"},
                         {"role": "assistant", "content": "c"}])
        self.assertEqual(m, [{"role": "user", "content": "a\n\nb"}, {"role": "assistant", "content": "c"}])
        long = [{"role": "user", "content": "開頭"}] + [
            {"role": r, "content": "x" * 1000} for r in ["assistant", "user"] * 50]
        t = trim_history(long, limit=10_000)
        self.assertEqual(t[0]["content"].split("\n\n")[0], "開頭")
        self.assertIn("較舊的對話因為太長而省略", t[0]["content"])
        self.assertLessEqual(sum(len(x["content"]) for x in t), 12_000)

    def test_answer_builds_full_history(self):
        async def go():
            llm = FakeLLM()
            cfg = N(ch_chat=10, ch_chat_prompt=11, tz="Asia/Taipei", llm_context=131072, chat_compact_pct=90)
            dc = N(client=N(user=N(id=BOT)))
            c = ChatChannel(cfg, dc, llm, {ME})
            c.memory = N(content="【關於使用者】\n- 資工系學生")
            starter = msg(100, ME, "這張圖是什麼", [Att(1, "a.png", "image/png")])
            hist = [msg(101, BOT, "這是一張貓的照片"), msg(102, BOT, "-# ⚠️ 狀態訊息"),
                    msg(103, ME, "/思考 那這份檔案呢", [Att(2, "note.txt", "text/plain", "檔案內容".encode(), 12)])]

            class Thread:
                id = 100
                parent_id = 10

                async def history(self, limit=None, oldest_first=True):
                    for h in hist:
                        yield h
            c._starters[100] = starter
            reply, user_text, usage, _ = await c.answer(Thread(), hist[-1])
            self.assertEqual(usage["total_tokens"], 1020)
            messages, thinking = llm.got
            self.assertTrue(thinking)
            self.assertEqual(user_text, "那這份檔案呢")
            self.assertEqual(messages[0]["role"], "system")
            self.assertIn("資工系學生", messages[0]["content"])
            self.assertEqual([m["role"] for m in messages[1:]], ["user", "assistant", "user"])
            self.assertEqual(messages[1]["content"][1]["type"], "image_url")      # 圖片交給模型
            self.assertIn("檔案內容", messages[3]["content"])                     # 文件抽出文字
            self.assertNotIn("狀態訊息", str(messages))                           # bot 的 -# 狀態不算對話
        asyncio.run(go())


class ProgressBarTest(unittest.TestCase):
    def test_bar(self):
        from chat import progress_bar
        self.assertEqual(progress_bar(0), "▱▱▱▱▱▱▱▱▱▱▱▱ 0%")
        self.assertEqual(progress_bar(0.5), "▰▰▰▰▰▰▱▱▱▱▱▱ 50%")
        self.assertEqual(progress_bar(1.2), "▰▰▰▰▰▰▰▰▰▰▰▰ 100%")


class ChatCompactTest(unittest.TestCase):
    def test_history_starts_from_summary_and_strips_footer(self):
        async def go():
            cfg = N(ch_chat=10, ch_chat_prompt=11, tz="Asia/Taipei", llm_context=131072, chat_compact_pct=90)
            llm = FakeLLM()
            c = ChatChannel(cfg, N(client=N(user=N(id=BOT))), llm, {ME})
            c.memory = N(content="")
            hist = [msg(101, BOT, "很久以前的回答\n-# 🧮 上下文 100,000／131,072 tokens（76%）"),
                    msg(102, BOT, "🗜️ **對話摘要（自動：用到 91%）**\n- 目標：做 InboxRelay\n-# 🗜️ 壓縮前約 119,000 tokens"),
                    msg(103, ME, "接著做 OCR"),
                    msg(104, BOT, "好\n-# 🧮 上下文 3,000／131,072 tokens（2%）"),
                    msg(105, ME, "然後呢")]

            class Thread:
                id = 100
                parent_id = 10

                async def history(self, limit=None, oldest_first=True):
                    for h in hist:
                        yield h
            c._starters[100] = msg(100, ME, "最一開始的問題")
            await c.answer(Thread(), hist[-1])
            messages, _ = llm.got
            self.assertIn("【先前對話摘要】", messages[0]["content"])
            self.assertIn("目標：做 InboxRelay", messages[0]["content"])
            body = str(messages[1:])
            self.assertNotIn("很久以前", body)           # 摘要之前的不再送出
            self.assertNotIn("最一開始的問題", body)
            self.assertNotIn("🧮", body)                 # 用量小字不算對話
            self.assertEqual([m["role"] for m in messages[1:]], ["user", "assistant", "user"])
        asyncio.run(go())


class ChatMemoryTest(unittest.TestCase):
    def test_split_memos(self):
        from chat import split_memos
        r, m = split_memos("答案在這裡\n- 第二點\n〔記憶〕使用者是資工系學生\n〔記憶〕偏好條列")
        self.assertEqual(r, "答案在這裡\n- 第二點")
        self.assertEqual(m, ["使用者是資工系學生", "偏好條列"])
        self.assertEqual(split_memos("沒有要記的"), ("沒有要記的", []))

    def test_remember_triggers(self):
        async def go():
            calls = []

            class Mem:
                observations, corrections = [], []

                def observe(self, t, kw, g):
                    self.observations.append(t)

                def add_correction(self, t):
                    self.corrections.append(t)

                async def upgrade(self, reason, urgent=False):
                    calls.append(urgent)
                    self.observations.clear()
                    self.corrections.clear()
            c = ChatChannel(N(ch_chat=1, ch_chat_prompt=2, tz="Asia/Taipei"), N(), FakeLLM(), set())
            c.memory = Mem()
            chat.IDLE_UPGRADE_SEC = 0.05
            await c._remember("今天天氣如何", [])              # 沒有值得記的 → 不升級
            await asyncio.sleep(0.1)
            self.assertEqual(calls, [])
            await c._remember("記住我是左撇子", [])            # 明確要求記住 → 馬上
            await asyncio.sleep(0.05)
            self.assertEqual(calls, [True])
            await c._remember("以後回答都用條列", ["使用者希望回答都用條列"])   # 改偏好＋模型標出 → 馬上
            await asyncio.sleep(0.05)
            self.assertEqual(calls, [True, True])
            await c._remember("我在做畢業專題", ["使用者正在做畢業專題"])   # 模型標出 → 安靜後整理
            await asyncio.sleep(0.2)
            self.assertEqual(calls, [True, True, True])
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()


class TitleTest(unittest.TestCase):
    def test_placeholder_and_clean(self):
        n = chat.placeholder_name("2026-10-04")
        self.assertTrue(chat.is_placeholder(n))
        self.assertFalse(chat.is_placeholder("2026-10-04_排查網路問題"))
        self.assertEqual(chat.clean_title("標題：「排查網路問題。」\n其他"), "排查網路問題")
        self.assertEqual(chat.clean_title(""), "")
        self.assertLessEqual(len(chat.clean_title("字" * 80)), 30)
