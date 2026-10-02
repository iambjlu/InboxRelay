# relay-core/tests/test_m4.py
"""M4：刪除同步探測、分析佇列、編輯同步（用假物件，不連網）。"""
import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from telegram.error import BadRequest  # noqa: E402

import records  # noqa: E402
from probe import Prober  # noqa: E402


class FakeBot:
    def __init__(self, deleted):
        self.deleted = set(deleted)
        self.copied, self.removed = [], []

    async def copy_message(self, chat_id, from_chat_id, message_id, disable_notification):
        if message_id in self.deleted:
            raise BadRequest("Message to copy not found")
        if message_id == 99:
            raise BadRequest("Message can't be copied")
        self.copied.append(message_id)
        return SimpleNamespace(message_id=1000 + message_id)

    async def delete_message(self, chat_id, message_id):
        self.removed.append(message_id)


class TestProbe(unittest.TestCase):
    def test_classify(self):
        res = {}

        async def alive(keys, url):
            res["alive"] = keys

        async def gone(keys, url, all_deleted):
            res["deleted"] = (keys, all_deleted)

        async def go():
            bot = FakeBot(deleted=[2])
            p = Prober(bot, -100, 0, alive, gone)
            p.schedule([(-1, 1), (-1, 2), (-1, 99)], "u", due=0)
            await asyncio.sleep(1.5)
            return bot

        bot = asyncio.run(go())
        self.assertEqual(res["deleted"], ([(-1, 2)], False))
        self.assertEqual(res["alive"], [(-1, 1), (-1, 99)])   # 不能複製的視為仍存在
        self.assertEqual(bot.removed, [1001])                  # 探測複本有刪掉


class FakeIndex:
    def __init__(self, recs):
        self.entries = {r.key: SimpleNamespace(msg_id=i, rec=r) for i, r in enumerate(recs)}
        self.updates = {}

    def __contains__(self, k):
        return k in self.entries

    def get(self, k):
        return self.entries.get(k)

    def by_mirror(self, url):
        return sorted((e for e in self.entries.values() if e.rec.mirror == url), key=lambda e: e.rec.tg_msg)

    async def update(self, key, **ch):
        import dataclasses
        self.entries[key].rec = dataclasses.replace(self.entries[key].rec, **ch)
        self.updates[key] = ch

    async def delete(self, key):
        self.entries.pop(key, None)


class FakeMsg:
    def __init__(self, content, attachments=()):
        self.content = content
        self.attachments = list(attachments)
        self.replies, self.edits = [], []
        self.channel = SimpleNamespace(send=self._send)
        self.id = 555

    async def reply(self, text, **kw):
        self.replies.append(text)
        return SimpleNamespace(jump_url="https://discord.com/channels/1/2/777")

    async def _send(self, text, **kw):
        self.replies.append(text)

    async def edit(self, **kw):
        self.edits.append(kw)
        self.content = kw.get("content") or ""


class FakeLLM:
    image_support = True
    memory = ""

    async def chat(self, prompt, data=None, **kw):
        if "JSON" in prompt:
            return '{"kw": ["蝦皮"], "gist": "待辦：編輯蝦皮"}'
        return "摘要內容"


def rec(msg, mirror="https://discord.com/channels/1/2/3", **kw):
    base = dict(tg_chat=-1, tg_msg=msg, origin="live", date="2026-10-02 10:00", type="text", mirror=mirror)
    base.update(kw)
    return records.KwRecord(**base)


class TestEnricher(unittest.TestCase):
    def test_text_only_no_reply_and_keywords(self):
        from enrich.pipeline import Enricher
        mirror = FakeMsg("# 2026-10-02\n編輯蝦皮")
        idx = FakeIndex([rec(5, excerpt="編輯蝦皮")])
        dc = SimpleNamespace(fetch_by_url=lambda u: _ret(mirror))
        cfg = SimpleNamespace(link_summary=True, image_summary=True, document_summary=True, keywords=True,
                              link_screenshot=False, ch_screenshot=None)
        e = Enricher(cfg, dc, FakeLLM(), idx, {}, None)
        asyncio.run(e.process(SimpleNamespace(keys=[(-1, 5)], mirror_url="u")))
        self.assertEqual(mirror.replies, [])                         # 純文字不回覆
        self.assertEqual(idx.updates[(-1, 5)]["kw"], "蝦皮")
        self.assertTrue(idx.updates[(-1, 5)]["enriched"])


async def _ret(x):
    return x


class TestEditSync(unittest.TestCase):
    def _relayer(self, mirror, recs):
        from relay import Relayer
        cfg = SimpleNamespace(edit_sync=True, edit_sync_hours=24, header_style="large", upload_limit_mb=20, tz="Asia/Taipei")
        dc = SimpleNamespace(fetch_by_url=lambda u: _ret(mirror), delete_by_url=lambda u: _ret(None))
        r = Relayer(cfg, dc, None, asyncio.Queue) if False else Relayer.__new__(Relayer)
        r.cfg, r.dc, r.index, r.text_cache = cfg, dc, FakeIndex(recs), {}
        r.dirty, r.enricher = None, SimpleNamespace(submit=lambda k, u: setattr(self, "submitted", (k, u)))
        return r

    def test_edit_keeps_header(self):
        from tg_intake import TgPiece
        mirror = FakeMsg("# 2026-10-02\n舊內容")
        r = self._relayer(mirror, [rec(7, excerpt="舊內容")])
        piece = TgPiece(-1, 7, datetime.now(timezone.utc), "新內容", "text")
        asyncio.run(r.handle_edit(piece))
        self.assertEqual(mirror.edits[-1]["content"], "# 2026-10-02\n新內容")
        self.assertEqual(r.index.updates[(-1, 7)]["enriched"], False)
        self.assertEqual(self.submitted[0], [(-1, 7)])

    def test_edit_too_old_ignored(self):
        from tg_intake import TgPiece
        mirror = FakeMsg("舊")
        r = self._relayer(mirror, [rec(8, excerpt="舊")])
        piece = TgPiece(-1, 8, datetime(2026, 9, 1, tzinfo=timezone.utc), "新", "text")
        asyncio.run(r.handle_edit(piece))
        self.assertEqual(mirror.edits, [])

    def test_import_not_synced(self):
        from tg_intake import TgPiece
        mirror = FakeMsg("x")
        r = self._relayer(mirror, [rec(9, origin="import")])
        asyncio.run(r.handle_edit(TgPiece(-1, 9, datetime.now(timezone.utc), "y", "text")))
        self.assertEqual(mirror.edits, [])


if __name__ == "__main__":
    unittest.main()


class TestDiscordDelete(unittest.TestCase):
    def test_mirror_deleted_removes_records(self):
        from relay import Relayer
        r = Relayer.__new__(Relayer)
        r.cfg = SimpleNamespace(ch_keywords=7, ch_inbox_mirror=2, header_style="large")
        deleted_urls = []
        r.dc = SimpleNamespace(delete_by_url=lambda u: _ret(deleted_urls.append(u)))
        r.index = FakeIndex([rec(1, mirror="https://discord.com/channels/1/2/500", excerpt="祕密",
                                 reply="https://discord.com/channels/1/2/501", enriched=True),
                             rec(2, mirror="https://discord.com/channels/1/2/600", excerpt="留著")])
        r.text_cache = {}
        marked = []
        r.dirty = SimpleNamespace(mark=lambda d: _ret(marked.append(d)))

        async def no_repair(*a, **k):
            return None
        r._repair_header = no_repair
        asyncio.run(r.on_discord_delete(2, 500, None))
        self.assertNotIn((-1, 1), r.index.entries)                                  # 紀錄整筆刪掉
        self.assertIn((-1, 2), r.index.entries)
        self.assertEqual(deleted_urls, ["https://discord.com/channels/1/2/501"])   # AI 回覆一起刪
        self.assertEqual(marked, ["2026-10-02"])                                    # 那天重新總結

    def test_keyword_record_deleted_forgets(self):
        from relay import Relayer
        r = Relayer.__new__(Relayer)
        r.cfg = SimpleNamespace(ch_keywords=7, ch_inbox_mirror=2)
        r.index = FakeIndex([rec(1)])
        asyncio.run(r.on_discord_delete(7, 0, None))   # FakeIndex 的 msg_id 從 0 開始
        self.assertNotIn((-1, 1), r.index.entries)


class TestAlbumMemory(unittest.TestCase):
    def test_album_sent_in_batches_with_bounded_memory(self):
        from relay import Relayer, plan_groups
        from tg_intake import AttachmentRef, RelayItem, TgPiece
        MB = 1024 * 1024
        atts = [AttachmentRef(f"f{i}", f"p{i}.jpg", 8 * MB, f"u{i}") for i in range(5)] + \
               [AttachmentRef("big", "huge.zip", 30 * MB, "ub")]
        self.assertEqual([len(g) for g in plan_groups(atts[:5], 20 * MB)], [2, 2, 1])

        live = {"now": 0, "peak": 0}
        sent = []

        class Bot:
            async def get_file(self, fid):
                async def dl():
                    live["now"] += 8 * MB
                    live["peak"] = max(live["peak"], live["now"])
                    return bytearray(8)
                return SimpleNamespace(file_size=8 * MB, download_as_bytearray=dl)

        async def send(ch, text, files=None, retries=3):
            live["now"] -= 8 * MB * len(files or [])
            sent.append((text, [n for n, _ in (files or [])]))
            return SimpleNamespace(id=len(sent), jump_url=f"https://discord.com/channels/1/2/{len(sent)}")

        r = Relayer.__new__(Relayer)
        r.cfg = SimpleNamespace(tz="Asia/Taipei", upload_limit_mb=20, header_style="large", ch_inbox_mirror=2,
                                ch_keywords=7)
        r.dc = SimpleNamespace(filesize_limit=lambda mb: 20 * MB, send=send)
        r.bot = Bot()
        r.index = FakeIndex([])
        r.index.add = lambda rec, msg: r.index.entries.__setitem__(rec.key, SimpleNamespace(msg_id=msg.id, rec=rec))
        r.text_cache, r.last_header_date, r.prober, r.enricher = {}, None, None, None
        pieces = [TgPiece(-1, i + 1, datetime.now(timezone.utc), "相簿" if i == 0 else "", "album", a)
                  for i, a in enumerate(atts)]
        asyncio.run(r.handle(RelayItem(pieces)))
        mirror_msgs = [x for x in sent if not (x[0] or "").startswith("KW|")]
        self.assertEqual([len(f) for _, f in mirror_msgs], [2, 2, 1])
        self.assertIn("📎 huge.zip（過大未搬移）", mirror_msgs[0][0])
        self.assertLessEqual(live["peak"], 16 * MB)        # 一次最多一批在記憶體
