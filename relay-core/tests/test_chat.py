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


class EditRegenTest(unittest.TestCase):
    """使用者編輯對話串裡自己發的最後一則 → 刪掉後面的回覆、用新內容重新生成。"""

    def _make(self, hist, parent=10):
        class FakeThread(discord.Thread):
            def __init__(self):
                self.id, self.parent_id, self.name = 200, parent, "手動命名"
                self.sent, self.deleted = [], []

            async def history(self, limit=None, after=None, oldest_first=True):
                for h in hist:
                    if after is None or h.id > after.id:
                        yield h

            async def fetch_message(self, mid):
                return next(h for h in hist if h.id == mid)

            async def send(self, content=None, **kw):
                m = msg(900 + len(self.sent), BOT, content)
                self.sent.append(content)
                hist.append(m)
                return m

            def typing(self):
                class T:
                    async def __aenter__(self_): return None
                    async def __aexit__(self_, *a): return False
                return T()
        return FakeThread()

    def test_edit_last_user_message_regenerates(self):
        async def go():
            cfg = N(ch_chat=10, ch_chat_prompt=11, tz="Asia/Taipei", llm_context=131072, chat_compact_pct=90)
            llm = FakeLLM()
            hist = [msg(1, ME, "第一題"), msg(2, BOT, "第一答\n-# 🧮 x"), msg(3, ME, "第二題（舊）"),
                    msg(4, BOT, "舊回答\n-# 🧮 x"), msg(5, BOT, "-# ⚠️ 舊的錯誤提示")]
            deleted = []
            for h in hist:
                async def d(h=h):
                    deleted.append(h.id)
                    hist.remove(h)
                h.delete = d
                h.type = discord.MessageType.default
            th = self._make(hist)
            thread_obj = th
            client = N(user=N(id=BOT), get_channel=lambda cid: thread_obj)
            c = ChatChannel(cfg, N(client=client), llm, {ME})
            c.memory = N(content="")
            c._remember = lambda *a: asyncio.sleep(0)
            hist[2].clean_content = "第二題（改過）"
            payload = N(channel_id=200, message_id=3, data={"edited_timestamp": "2026-10-05T10:00:00+00:00"})
            await c.on_edit(payload)
            self.assertEqual(deleted, [4, 5])                                   # 舊回覆與舊提示都刪
            self.assertEqual(len(th.sent), 1)                                   # 重新發了一則回覆
            self.assertIn("好的", th.sent[0])
            sent_msgs = str(llm.got[0])
            self.assertIn("第二題（改過）", sent_msgs)
            self.assertNotIn("舊回答", sent_msgs)
            await c.on_edit(payload)                                            # 同一次編輯不重複觸發
            self.assertEqual(len(th.sent), 1)
        asyncio.run(go())

    def test_edit_not_last_or_not_real_edit_is_ignored(self):
        async def go():
            cfg = N(ch_chat=10, ch_chat_prompt=11, tz="Asia/Taipei", llm_context=131072, chat_compact_pct=90)
            llm = FakeLLM()
            hist = [msg(1, ME, "第一題"), msg(2, BOT, "第一答"), msg(3, ME, "第二題"), msg(4, BOT, "第二答")]
            for h in hist:
                h.type = discord.MessageType.default
                h.delete = lambda: asyncio.sleep(0)
            th = self._make(hist)
            client = N(user=N(id=BOT), get_channel=lambda cid: th)
            c = ChatChannel(cfg, N(client=client), llm, {ME})
            c.memory = N(content="")
            await c.on_edit(N(channel_id=200, message_id=1, data={"edited_timestamp": "t"}))   # 不是最後一則
            await c.on_edit(N(channel_id=200, message_id=3, data={}))                          # 只是連結預覽更新
            self.assertEqual(th.sent, [])
            self.assertIsNone(llm.got)
            hist.insert(3, msg(40, BOT, "🗜️ **對話摘要（手動）**\n- x"))                    # 後面已壓縮
            await c.on_edit(N(channel_id=200, message_id=3, data={"edited_timestamp": "t2"}))
            self.assertTrue(th.sent and "壓縮" in th.sent[0])
            self.assertIsNone(llm.got)
        asyncio.run(go())


class ThreadDeleteAndTitleTest(EditRegenTest):
    def test_deleting_thread_cancels_pending_llm_request(self):
        async def go():
            cfg = N(ch_chat=10, ch_chat_prompt=11, tz="Asia/Taipei", llm_context=131072, chat_compact_pct=90)
            started, cancelled = asyncio.Event(), []

            class HangLLM(FakeLLM):
                async def chat_messages(self, messages, max_tokens=4000, thinking=False, progress=None):
                    started.set()
                    try:
                        await asyncio.sleep(30)                                   # 模型還沒回來
                    except asyncio.CancelledError:
                        cancelled.append(True)
                        raise
            hist = [msg(1, ME, "問題")]
            hist[0].type = discord.MessageType.default
            th = self._make(hist)
            hist[0].channel = th
            c = ChatChannel(cfg, N(client=N(user=N(id=BOT))), HangLLM(), {ME})
            c.memory = N(content="")
            task = asyncio.create_task(c.on_message(hist[0]))
            await asyncio.wait_for(started.wait(), 2)
            await c.on_thread_delete(200, 99)                                      # 別的頻道的討論串：不動
            self.assertFalse(task.done())
            await c.on_thread_delete(200, 10)                                      # 對話串被刪
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertEqual(cancelled, [True])                                    # LLM 請求被取消
            self.assertEqual(th.sent, [])                                          # 也不會再往已刪除的對話串發東西
            self.assertEqual(c._work, {})
        asyncio.run(go())

    def test_retitle_posts_notice_then_deletes_it(self):
        async def go():
            cfg = N(ch_chat=10, ch_chat_prompt=11, tz="Asia/Taipei", llm_context=131072, chat_compact_pct=90)
            events = []

            class TitleLLM(FakeLLM):
                async def chat(self, *a, **k):
                    events.append("llm")
                    return "旅遊規劃"
            hist = []
            th = self._make(hist)

            async def edit(name=None):
                events.append("edit")
                th.name = name
            th.edit = edit
            orig_send = th.send

            async def send(content=None, **kw):
                m = await orig_send(content, **kw)
                events.append("notice")
                async def d():
                    events.append("deleted")
                    hist.remove(m)
                m.delete = d
                return m
            th.send = send
            c = ChatChannel(cfg, N(client=N(user=N(id=BOT))), TitleLLM(), {ME})
            await c._retitle(th, "幫我規劃旅遊", "好的")
            self.assertEqual(events, ["notice", "llm", "edit", "deleted"])        # 先提示、改完名才刪提示
            self.assertEqual(th.name, "旅遊規劃")
            self.assertIn("正在重新命名", th.sent[0])
            self.assertEqual(hist, [])
        asyncio.run(go())


class QueueTest(EditRegenTest):
    def test_queued_messages_answered_once_and_late_one_answered_next(self):
        async def go():
            cfg = N(ch_chat=10, ch_chat_prompt=11, tz="Asia/Taipei", llm_context=131072, chat_compact_pct=90)
            gate, calls = asyncio.Event(), []

            class SlowLLM(FakeLLM):
                async def chat_messages(self, messages, max_tokens=4000, thinking=False, progress=None):
                    calls.append(messages)
                    if len(calls) == 1:
                        await gate.wait()                                         # 第一次回覆生成中
                    return f"回覆{len(calls)}", {}
            hist = []
            th = self._make(hist)
            orig_send = th.send
            nid = [100]

            async def send(content=None, **kw):
                m = msg(nid[0], BOT, content)
                nid[0] += 1
                m.type = discord.MessageType.default
                hist.append(m)
                th.sent.append(content)
                return m
            th.send = send

            def user(i, text):
                m = msg(i, ME, text)
                m.type = discord.MessageType.default
                m.channel = th
                hist.append(m)
                return m
            c = ChatChannel(cfg, N(client=N(user=N(id=BOT))), SlowLLM(), {ME})
            c.memory = N(content="")
            c._wants_title = lambda t: False
            m1, m2, m3 = user(1, "問題一"), user(2, "問題二"), user(3, "問題三")
            tasks = [asyncio.create_task(c.on_message(m)) for m in (m1, m2, m3)]
            await asyncio.sleep(0.05)                                             # 2、3 在排隊
            m4 = user(4, "生成中才傳的問題四")                                    # 第一次回覆的歷史快照之後才來
            tasks.append(asyncio.create_task(c.on_message(m4)))
            await asyncio.sleep(0.05)
            gate.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
            self.assertEqual(len(calls), 2)                                       # 一～三合併回一次；第四則另外回
            first = str(calls[0])
            self.assertTrue("問題一" in first and "問題二" in first and "問題三" in first)
            self.assertNotIn("問題四", first)
            second = calls[1]
            roles = [x["role"] for x in second if x["role"] != "system"]
            self.assertEqual(roles, ["user", "assistant", "user"])                # 問題四排在第一次回覆之後
            self.assertIn("問題四", str(second[-1]))
            self.assertEqual(len(th.sent), 2)
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

                async def send(self, content=None, **kw):              # 「正在重新命名…」提示
                    renamed.append("notice")
                    return N(delete=lambda: asyncio.sleep(0))
            t = Thread()
            self.assertTrue(c._wants_title(t))
            await c._retitle(t, "早安", "早安！")
            self.assertIs(llm.prompt, chat.TITLE_PROMPT)
            self.assertEqual(renamed, ["notice", "早安問候與週日計畫"])
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

                @property
                def jump_url(self):
                    return f"https://discord.com/channels/1/{self.id}"

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
            self.assertEqual([b.label for b in view.children], ["開啟聊天", "新增聊天", "從這裡分支", "刪除此對話"])
            self.assertEqual(view.children[0].url, "https://discord.com/channels/1/500")   # 最左邊是打開對話串的連結
            self.assertTrue(chat.ThreadControlView(c).is_persistent())                    # 註冊用的版本仍可持久化
            t = await c._target_thread(N(channel=parent, message=N(content=content)))
            self.assertIs(t, threads[500])                                      # 按鈕找得到對應的對話串
        asyncio.run(go())
