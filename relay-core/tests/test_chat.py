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
        self.assertRegex(n, r"^2026-10-04_[a-z]+-[a-z]+-\d{4}$")
        self.assertTrue(chat.is_placeholder("2026-10-04_black-dog-8818"))
        self.assertTrue(chat.is_placeholder("2026-10-04_藍色-企鵝-4821"))
        self.assertTrue(chat.is_placeholder("2026-10-04_k7x2"))              # 舊格式
        self.assertFalse(chat.is_placeholder("2026-10-04_排查網路問題"))
        self.assertEqual(chat.clean_title("標題：「排查網路問題。」\n其他"), "排查網路問題")
        self.assertEqual(chat.clean_title(""), "")
        self.assertLessEqual(len(chat.clean_title("字" * 80)), 30)


class BranchTest(unittest.TestCase):
    def test_branch_copies_visibly_but_llm_reads_only_compacted_part(self):
        async def go():
            cfg = N(ch_chat=10, ch_chat_prompt=11, tz="Asia/Taipei", llm_context=131072, chat_compact_pct=90)
            llm = FakeLLM()
            c = ChatChannel(cfg, N(client=N(user=N(id=BOT))), llm, {ME})
            c.memory = N(content="")
            src_hist = [msg(101, ME, "很久以前的問題"), msg(102, BOT, "很久以前的回答"),
                        msg(103, BOT, "🗜️ **對話摘要（手動）**\n- 目標：做 InboxRelay"),
                        msg(104, ME, "接著做 OCR", [Att(7, "x.png", "image/png", b"png", 3)]),
                        msg(105, BOT, "好\n-# 🧮 上下文 3,000／131,072 tokens（2%）"),
                        msg(106, BOT, "-# 💬 控制訊息")]
            for m in src_hist:
                m.author.display_name = "我"

            class Src:
                id = 100
                parent_id = 10

                async def history(self, limit=None, oldest_first=True):
                    for h in src_hist:
                        yield h

            posted = []

            class Dst:
                id = 200
                parent_id = 10
                guild = N(filesize_limit=10 * 1024 * 1024)

                async def send(self, content=None, files=(), view=None):
                    m = msg(300 + len(posted), BOT, content)
                    m.attachments = [Att(900 + len(posted), f.filename, "image/png", b"png", 3) for f in files]
                    posted.append(m)

                async def history(self, limit=None, oldest_first=True):
                    for h in posted:
                        yield h
            dst = Dst()
            for h in await c._collect(Src()):
                await c._copy_message(dst, h)
            self.assertEqual(len(posted), 5)                                   # 控制訊息不搬
            self.assertTrue(posted[0].content.startswith("👤 **我**\n很久以前的問題"))   # 使用者發言帶標記
            self.assertEqual(len(posted[3].attachments), 1)                    # 圖片一起搬
            new = msg(400, ME, "再問一題")
            await c.answer(dst, new)
            messages, _ = llm.got
            joined = str(messages)
            self.assertNotIn("很久以前", joined)                                 # LLM 只讀摘要之後
            self.assertIn("目標：做 InboxRelay", messages[0]["content"])
            self.assertEqual([m["role"] for m in messages[1:]], ["user", "assistant", "user"])
            self.assertEqual(messages[1]["content"][0]["text"].split("\n")[0], "接著做 OCR")
        asyncio.run(go())

    def test_no_compact_llm_reads_everything(self):
        async def go():
            cfg = N(ch_chat=10, ch_chat_prompt=11, tz="Asia/Taipei", llm_context=131072, chat_compact_pct=90)
            llm = FakeLLM()
            c = ChatChannel(cfg, N(client=N(user=N(id=BOT))), llm, {ME})
            c.memory = N(content="")
            posted = [msg(1, BOT, "👤 **我**\n你好"), msg(2, BOT, "嗨")]

            class Dst:
                id = 200
                parent_id = 10

                async def history(self, limit=None, oldest_first=True):
                    for h in posted:
                        yield h
            await c.answer(Dst(), msg(3, ME, "繼續"))
            messages, _ = llm.got
            self.assertEqual([m["role"] for m in messages[1:]], ["user", "assistant", "user"])
            self.assertIn("你好", str(messages))
        asyncio.run(go())


def _cfg():
    return N(ch_chat=10, ch_chat_prompt=11, tz="Asia/Taipei", llm_context=131072, chat_compact_pct=90)


class RetitleTest(unittest.TestCase):
    def test_placeholder_thread_gets_ai_title(self):
        async def go():
            class LLM:
                image_support = True

                async def chat(self, prompt, data=None, max_tokens=800, use_memory=True, **k):
                    self.prompt = prompt
                    return "標題：早安問候與週日計畫。"
            llm = LLM()
            c = ChatChannel(_cfg(), N(client=N(user=N(id=BOT))), llm, {ME})
            renamed = []

            class Thread:
                id = 300
                name = "2026-10-04_orange-grape-6661"
                created_at = None

                async def edit(self, name):
                    renamed.append(name)
            t = Thread()
            self.assertTrue(c._wants_title(t))
            await c._retitle(t, "早安", "早安！")
            self.assertIs(llm.prompt, chat.TITLE_PROMPT)
            self.assertEqual(renamed, ["2026-10-04_早安問候與週日計畫"])
        asyncio.run(go())


class ControlsTest(unittest.TestCase):
    def test_new_chat_posts_controls_in_main_channel_and_buttons_find_thread(self):
        async def go():
            sent = []

            class Thread(discord.Thread):
                def __init__(self, tid):
                    self.id, self.parent_id, self.name = tid, 10, "t"

                @property
                def mention(self):
                    return f"<#{self.id}>"

            threads = {}

            class Parent:
                id = 10

                async def create_thread(self, name, type=None, auto_archive_duration=None):
                    t = Thread(500 + len(threads))
                    threads[t.id] = t
                    return t

                async def send(self, content, view=None):
                    sent.append((content, view))
                    return N(content=content)
            parent = Parent()

            async def channel(i):
                return parent
            client = N(user=N(id=BOT), get_channel=lambda i: threads.get(i))
            c = ChatChannel(_cfg(), N(client=client, channel=channel), None, {ME})

            async def defer(*a, **k):
                pass
            inter = N(channel=parent, response=N(defer=defer), message=None)
            await c.open_thread(inter)
            content, view = sent[-1]
            self.assertEqual(content, "✅ 已開新對話\n<#500>")                 # 主頻道、換行、不是回覆
            self.assertIsInstance(view, chat.ThreadControlView)                 # 三顆按鈕在這則訊息下面
            self.assertEqual({b.label for b in view.children}, {"新增聊天", "從這裡分支", "刪除此對話"})
            t = await c._target_thread(N(channel=parent, message=N(content=content)))
            self.assertIs(t, threads[500])                                      # 按鈕找得到對應的對話串
        asyncio.run(go())
