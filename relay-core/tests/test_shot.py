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
    return "binary" if u.endswith(".png") else ""


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
            self.assertEqual(tokens[2], SKIP)                                  # 無法預覽的檔案不截圖
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

    def test_add_arrows_idempotent(self):
        content = "# 2025-07-16\n看這個 https://a.example/x 還有 https://b.example/y\n沒連結\nhttps://c.example/z\n-# [src](<https://t.me/c/1/2>)"
        links = {"https://a.example/x": "https://discord.com/channels/1/5/11", "https://b.example/y": "https://discord.com/channels/1/5/12"}
        out = shotworker.add_arrows(content, links)
        lines = out.split("\n")
        self.assertEqual(lines[2], "-# [↗ 截圖](<https://discord.com/channels/1/5/11>) [↗ 截圖](<https://discord.com/channels/1/5/12>)")
        self.assertEqual(len(lines), len(content.split("\n")) + 1)             # 只多一行；沒截圖的連結不加
        self.assertEqual(shotworker.add_arrows(out, links), out)               # 重複執行結果一樣
        self.assertEqual(records.mirror_texts([rec(1, "看這個 https://a.example/x")], out)[(-1, 1)].split()[0], "看這個")   # 不影響切全文

    def test_sync_arrows_edits_own_mirror(self):
        async def go():
            link = "https://discord.com/channels/1/5/" + str(now_id())
            text = "看 https://a.example/x"
            edits = []

            class Mir:
                content = text
                author = N(id=7)

                async def edit(self, content):
                    edits.append(content)
            dc = Posts(text)
            dc.mirror = Mir()
            dc.client = N(user=N(id=7))
            dc.posts[link] = N(content="📸 2025-07-16 13:12｜https://a.example/x\n…")
            idx = Index([rec(1, text, shot=link)])
            w = ShotWorker(N(ch_screenshot=5), dc, idx, {})
            real_sleep = asyncio.sleep

            async def fast_sleep(_):
                await real_sleep(0)
            with mock.patch.object(shotworker.asyncio, "sleep", fast_sleep):
                self.assertTrue(await w.sync_arrows(MIRROR))
            self.assertEqual(edits, [f"{text}\n-# [↗ 截圖](<{link}>)"])
            dc.mirror.author = N(id=8)                                           # 別人（使用者）發的不改
            self.assertFalse(await w.sync_arrows(MIRROR))
        asyncio.run(go())

    def test_classify_content(self):
        from enrich.link import classify_content as cc
        for t in ("text/html; charset=utf-8", "application/json", "application/ld+json", "application/vnd.api+json",
                  "application/xml", "text/xml", "application/rss+xml", "application/atom+xml", "image/svg+xml",
                  "application/pdf", "text/plain", "text/csv", "application/javascript", "application/x-yaml",
                  "image/heic", ""):
            self.assertEqual(cc(t), "", t)
        for t in ("application/zip", "video/mp4", "application/octet-stream", "audio/mpeg", "application/x-apple-diskimage"):
            self.assertEqual(cc(t), "binary", t)
        self.assertEqual(cc("application/pdf", 200 * 1024 * 1024), "binary")                 # 太大

    def test_retry_unrenderable_failures(self):
        async def go():
            import tempfile
            link = "https://discord.com/channels/1/5/" + str(now_id())
            other = "https://discord.com/channels/1/5/" + str(now_id() + 1)
            dc = Posts("x")
            dc.posts[link] = N(content="📸 無法截圖｜t｜<https://a/x.json>\n-# 原因：不是可截圖的網頁（application/json，0 MB）\n…")
            dc.posts[other] = N(content="📸 無法截圖｜t｜<https://b/y>\n-# 原因：逾時\n…")
            idx = Index([rec(1, "https://a/x.json https://b/y", shot=f"!{link} !{other}")])
            w = ShotWorker(N(ch_screenshot=5), dc, idx, {})
            with tempfile.TemporaryDirectory() as d, mock.patch("paths.data_dir", lambda: Path(d)):
                await w.retry_unrenderable_failures()
            toks = idx.updates[(-1, 1)]["shot"].split()
            self.assertEqual(toks, [f"?0{link}", f"!{other}"])                                # 只改 JSON 那個，逾時的不動
            self.assertTrue(w.needs_shot(idx.entries[(-1, 1)]))                               # 排進積欠
        asyncio.run(go())

    def test_reset_skipped_keeps_other_tokens(self):
        async def go():
            import tempfile
            ok = "https://discord.com/channels/1/5/" + str(now_id())
            idx = Index([rec(1, "https://a/x.yaml https://b/y", shot=f"- {ok}"), rec(2, "沒連結的長文字"), rec(3, "https://c/z", shot="-")])
            w = ShotWorker(N(ch_screenshot=5), Posts("x"), idx, {})
            with tempfile.TemporaryDirectory() as d, mock.patch("paths.data_dir", lambda: Path(d)):
                await w.reset_skipped()
            self.assertEqual(idx.updates[(-1, 1)]["shot"], ok)                                # 只拿掉「不截圖」，成功的留著
            self.assertIsNone(idx.updates[(-1, 3)]["shot"])
            self.assertNotIn((-1, 2), idx.updates)
        asyncio.run(go())

    def test_parse_notice_and_view(self):
        from enrich import shotbutton
        txt = "📸 無法截圖｜2026-08-22 12:29｜<https://a.example/x?cid=1>\n-# 原因：網域不存在（DNS 查不到）\n[↗ 跳到鏡像](<https://discord.com/channels/1/2/3>)"
        self.assertEqual(shotbutton.parse_notice(txt), ("2026-08-22 12:29", "https://a.example/x?cid=1", "https://discord.com/channels/1/2/3"))
        self.assertIsNone(shotbutton.parse_notice("📸 2026-08-22 12:29｜https://a.example/x\n[↗ 跳到鏡像](<https://discord.com/channels/1/2/3>)"))

        async def go():
            v = shotbutton.RetryView()
            self.assertEqual([c.custom_id for c in v.children], [shotbutton.CUSTOM_ID])
        asyncio.run(go())

    def test_retry_notice_success_replaces_token_and_deletes_notice(self):
        async def go():
            old = "https://discord.com/channels/1/5/" + str(now_id())
            notice = N(jump_url=old, deleted=False,
                       content=f"📸 無法截圖｜2025-07-16 13:12｜<https://a.example/x>\n-# 原因：逾時\n[↗ 跳到鏡像](<{MIRROR}>)")

            async def delete():
                notice.deleted = True
            notice.delete = delete
            dc = Posts("x")
            sent = []

            async def send_file(ch, content, name, data):
                sent.append((ch, content))
                return N(jump_url="https://discord.com/channels/1/5/" + str(now_id() + 7))
            dc.send_file = send_file
            idx = Index([rec(1, "https://a.example/x", shot=f"!{old}", reply=None)])
            w = ShotWorker(N(ch_screenshot=5), dc, idx, {})
            arrows = []

            async def fake_arrows(m, msg=None):
                arrows.append(m)
                return True
            w.sync_arrows = fake_arrows

            async def ok(url, tries=1):
                return b"jpg", ""
            with mock.patch.object(screenshot, "try_capture", ok):
                text = await w.retry_notice(notice)
            self.assertTrue(text.startswith("✅"))
            new_tok = idx.updates[(-1, 1)]["shot"]
            self.assertFalse(new_tok.startswith(("!", "?")))                           # 失敗記號換成成功的截圖連結
            self.assertNotEqual(new_tok, old)
            self.assertTrue(notice.deleted)                                            # 舊通知刪掉
            self.assertEqual(arrows, [MIRROR])                                         # 鏡像補小箭頭
            self.assertIn("https://a.example/x", sent[0][1])
        asyncio.run(go())

    def test_retry_notice_failure_updates_reason_and_network_down_is_reported(self):
        async def go():
            edits = []
            notice = N(jump_url="https://discord.com/channels/1/5/9",
                       content=f"📸 無法截圖｜t｜<https://a.example/x>\n-# 原因：逾時\n[↗ 跳到鏡像](<{MIRROR}>)")

            async def edit(content):
                edits.append(content)
            notice.edit = edit
            idx = Index([rec(1, "https://a.example/x", shot="!https://discord.com/channels/1/5/9")])
            w = ShotWorker(N(ch_screenshot=5), Posts("x"), idx, {})

            async def fail(url, tries=1):
                return None, "網域不存在（DNS 查不到）"
            with mock.patch.object(screenshot, "try_capture", fail):
                text = await w.retry_notice(notice)
            self.assertTrue(text.startswith("❌") and "網域不存在" in text)
            self.assertIn("-# 原因：網域不存在（DNS 查不到）", edits[0])
            self.assertEqual(idx.updates, {})                                          # 失敗：紀錄不動

            async def down(url, tries=1):
                raise screenshot.NetworkDown("x")
            with mock.patch.object(screenshot, "try_capture", down):
                self.assertIn("網路", await w.retry_notice(notice))
            self.assertEqual(await w.retry_notice(N(content="隨便")), "這不是截圖失敗通知，沒辦法重試")
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
