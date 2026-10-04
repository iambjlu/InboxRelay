# relay-core/tests/test_shot.py
import asyncio
import dataclasses
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import records  # noqa: E402
from enrich import screenshot, shotworker  # noqa: E402
from enrich.shotworker import SKIP, ShotWorker  # noqa: E402

MIRROR = "https://discord.com/channels/1/2/3"


def rec(msg, text, shot=None, reply=None):
    return records.KwRecord(-1, msg, "import", "2025-07-16 13:12", "text", MIRROR,
                            excerpt=records.make_excerpt(text), shot=shot, reply=reply)


class Index:
    def __init__(self, recs):
        self.entries = {r.key: N(rec=r, msg_id=r.tg_msg) for r in recs}
        self.updates = {}

    def by_mirror(self, url):
        return [e for e in self.entries.values() if e.rec.mirror == url]

    async def update(self, key, **kw):
        self.updates[key] = kw
        e = self.entries[key]
        e.rec = dataclasses.replace(e.rec, **kw)


LONG = ("CloudFront版連結\n" + "說明" * 90 + "\n待辦事項：https://dead.example/\n佈告欄：https://dead.example/gong-gao.php\n"
        "圖片：https://img.example/a.png\nhttps://ithelp.example/2525")


class Posts:
    """假的 Discord：記錄發出的貼文，fetch_by_url 拿得到貼文內容。"""
    def __init__(self, mirror_content, reply=None):
        self.mirror = N(content=mirror_content)
        self.reply = reply
        self.posts = {}

    async def fetch_by_url(self, url):
        if url == MIRROR:
            return self.mirror
        if self.reply is not None and url == "https://discord.com/channels/1/2/77":
            return self.reply
        return self.posts.get(url)


def now_id():
    import time
    return int((time.time() * 1000 - 1420070400000)) << 22      # 現在的 Discord 訊息 ID


def fake_post_one(dc):
    async def post_one(dc_, ch, url, when, back, **k):
        link = f"https://discord.com/channels/1/5/{now_id() + len(dc.posts) + 1}"
        ok = "dead" not in url
        dc.posts[link] = N(content=(f"📸 {when}｜{url}" if ok else f"📸 無法截圖｜{when}｜<{url}>\n-# 原因：網域不存在") + "\n…")
        return link, ok, ("" if ok else "網域不存在（DNS 查不到）")
    return post_one


async def probe(u):
    return "image" if u.endswith(".png") else ""


class ShotTest(unittest.TestCase):
    def test_scan_queues_unhandled_links(self):
        async def go():
            idx = Index([rec(1, "看 https://a.example/x"), rec(2, "沒有連結"),
                         rec(3, "https://b.example/", shot="https://discord.com/channels/1/9/9")])
            w = ShotWorker(N(ch_screenshot=5), None, idx, {})
            self.assertEqual(w.scan_index(), 1)
            self.assertEqual(list(w.queue), [MIRROR])
            self.assertEqual(w.scan_index(), 0)          # 已在佇列不重複
        asyncio.run(go())

    def test_every_link_from_full_text_gets_shot_or_failure_notice(self):
        async def go():
            r = rec(1, LONG, reply="https://discord.com/channels/1/2/77")
            self.assertEqual(shotworker.find_urls(r.excerpt), [])               # 摘錄裡一個網址都沒有
            idx = Index([r])
            edits = []

            class Reply:
                content = "**🔗 https://ithelp.example/2525**\n> 摘要\n📸 截圖：[↗1](<old>)"

                async def edit(self, content):
                    edits.append(content)
            dc = Posts("# 2025-07-16\n" + LONG + "\n-# [src](<https://t.me/c/1/2>)", Reply())
            with mock.patch.object(shotworker, "probe_kind", probe), \
                 mock.patch.object(screenshot, "available", lambda: True), \
                 mock.patch.object(screenshot, "post_one", fake_post_one(dc)):
                w = ShotWorker(N(ch_screenshot=5), dc, idx, {})
                self.assertTrue(w.needs_shot(idx.entries[r.key]))
                self.assertTrue(await w.process(MIRROR))
            tokens = idx.updates[r.key]["shot"].split()
            self.assertEqual(len(tokens), 4)                                   # 4 個網址各一個記號（不含 t.me 來源）
            self.assertTrue(tokens[0].startswith("?1") and tokens[1].startswith("?1"))   # 網域不存在 → 無法截圖通知（之後會再試）
            self.assertEqual(tokens[2], SKIP)                                  # 圖片網址不截圖
            self.assertFalse(tokens[3].startswith(("!", "?")))                 # 成功
            self.assertEqual(len(dc.posts), 3)
            self.assertFalse(w.needs_shot(idx.entries[r.key]))                 # 全部處理過 → 不再排隊
            self.assertEqual(edits[-1].count("📸 截圖："), 1)                   # 回覆的截圖行換成新的
            self.assertTrue(edits[-1].endswith(f"[↗1](<{tokens[3]}>)"))
        asyncio.run(go())

    def test_failed_link_retried_then_final(self):
        async def go():
            text = "https://dead.example/x"
            dc = Posts(text)
            dc.deleted = []

            async def delete_by_url(u):
                dc.deleted.append(u)
            dc.delete_by_url = delete_by_url
            old = "https://discord.com/channels/1/5/50"
            dc.posts[old] = N(content="📸 無法截圖｜2025-07-16 13:12｜<https://dead.example/x>\n-# 原因：網域不存在\n…")
            idx = Index([rec(1, text, shot="?0" + old)])                       # 清理舊失敗標的「馬上重試」
            with mock.patch.object(shotworker, "probe_kind", probe), \
                 mock.patch.object(screenshot, "available", lambda: True), \
                 mock.patch.object(screenshot, "post_one", fake_post_one(dc)):
                w = ShotWorker(N(ch_screenshot=5), dc, idx, {})
                self.assertTrue(w.needs_shot(idx.entries[(-1, 1)]))
                await w.process(MIRROR)
                tok = idx.updates[(-1, 1)]["shot"]
                self.assertTrue(tok.startswith("?1"))                            # 又失敗：第 1 次
                self.assertEqual(dc.deleted, [old])                              # 舊通知換掉
                self.assertEqual(len(tok.split()), 1)
                self.assertFalse(w.needs_shot(idx.entries[(-1, 1)]))             # 剛失敗，要隔一段時間才再試
                idx.entries[(-1, 1)].rec = dataclasses.replace(idx.entries[(-1, 1)].rec, shot="?2" + old)
                with mock.patch.object(shotworker, "retry_due", lambda t: True):
                    await w.process(MIRROR)
            self.assertTrue(idx.updates[(-1, 1)]["shot"].startswith("!"))        # 第 3 次還是失敗 → 最終失敗
        asyncio.run(go())

    def test_parse_token(self):
        self.assertEqual(shotworker.parse_token("-"), ("skip", 0, ""))
        self.assertEqual(shotworker.parse_token("!https://x/1/2/3"), ("fail", shotworker.RETRY_MAX, "https://x/1/2/3"))
        self.assertEqual(shotworker.parse_token("?2https://x/1/2/3"), ("retry", 2, "https://x/1/2/3"))
        self.assertEqual(shotworker.parse_token("https://x/1/2/3"), ("ok", 0, "https://x/1/2/3"))
        self.assertEqual(shotworker.success_links("-  !a ?1b https://x/1/2/3"), ["https://x/1/2/3"])

    def test_old_partial_record_reuses_existing_shot(self):
        async def go():
            text = "https://a.example/1 https://b.example/2"
            dc = Posts(text)
            dc.posts["https://discord.com/channels/1/5/99"] = N(content="📸 2025-07-16 13:12｜https://b.example/2\n…")
            idx = Index([rec(1, text, shot="https://discord.com/channels/1/5/99")])     # 舊版只拍了第二個
            with mock.patch.object(shotworker, "probe_kind", probe), \
                 mock.patch.object(screenshot, "available", lambda: True), \
                 mock.patch.object(screenshot, "post_one", fake_post_one(dc)):
                w = ShotWorker(N(ch_screenshot=5), dc, idx, {})
                await w.process(MIRROR)
            tokens = idx.updates[(-1, 1)]["shot"].split()
            self.assertEqual(tokens[1], "https://discord.com/channels/1/5/99")   # 沿用，不重拍
            self.assertEqual(len(dc.posts), 2)                                   # 只補拍第一個
        asyncio.run(go())

    def test_long_text_without_links_is_marked_once(self):
        async def go():
            text = "很長的文字" * 60
            dc = Posts(text)
            idx = Index([rec(1, text)])
            with mock.patch.object(screenshot, "available", lambda: True):
                w = ShotWorker(N(ch_screenshot=5), dc, idx, {})
                self.assertTrue(w.needs_shot(idx.entries[(-1, 1)]))      # 摘錄被截斷，要讀鏡像確認
                await w.process(MIRROR)
            self.assertEqual(idx.updates[(-1, 1)]["shot"], SKIP)
            self.assertFalse(w.needs_shot(idx.entries[(-1, 1)]))         # 之後不再讀鏡像
        asyncio.run(go())

    def test_ensure_jumps_queue_and_waits(self):
        async def go():
            idx = Index([rec(1, "https://a.example/x")])
            w = ShotWorker(N(ch_screenshot=5), None, idx, {})
            w.submit("other")
            waiter = asyncio.create_task(w.ensure(MIRROR, timeout=5))
            await asyncio.sleep(0.01)
            self.assertEqual(list(w.queue), [MIRROR, "other"])      # 插隊到最前面
            self.assertFalse(waiter.done())
            w._release(MIRROR)
            await asyncio.wait_for(waiter, 1)
            await asyncio.wait_for(ShotWorker(N(), None, Index([rec(2, "沒連結")]), {}).ensure(MIRROR), 1)
        asyncio.run(go())


class MirrorTextsTest(unittest.TestCase):
    def test_split_per_record_and_skip_meta_lines(self):
        r1, r2 = rec(1, LONG), rec(2, "下一則 https://b.example/x")
        content = "# 2025-07-16\n" + LONG + "\n下一則 https://b.example/x\n📎 a.zip（過大未搬移）\n-# [src](<https://t.me/c/1/2>)"
        out = records.mirror_texts([r1, r2], content)
        self.assertEqual(shotworker.find_urls(out[r1.key])[-1], "https://ithelp.example/2525")
        self.assertEqual(shotworker.find_urls(out[r2.key]), ["https://b.example/x"])


if __name__ == "__main__":
    unittest.main()
