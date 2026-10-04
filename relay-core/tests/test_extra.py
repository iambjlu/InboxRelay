# relay-core/tests/test_extra.py
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from importer.extra import doc_key, invoice_docs, parse_invoice_text, post_chunks, scan  # noqa: E402

COMMA = """﻿表頭=M,載具名稱,載具號碼,發票日期,商店統編,商店店名,發票號碼,總金額,發票狀態,
明細=D,發票號碼,小計,品項名稱,
M,手機條碼,/-ABC,20260501,1,統一超商股份有限公司台南分公司,AU1,70,開立,
D,AU1,60,飯糰,
D,AU1,50,炭火燒肉飯糰,
D,AU1,-40,折扣,
M,手機條碼,/-ABC,20260501,2,全家便利商店股份有限公司,AG2,23,開立,
D,AG2,23,泡麵,
M,手機條碼,/-ABC,20260502,3,某店,ZZ3,99,作廢,
D,ZZ3,99,作廢的東西,
"""
PIPE = COMMA.replace(",", "|")


class InvoiceTest(unittest.TestCase):
    def test_both_delimiters(self):
        for text in (COMMA, PIPE):
            docs = invoice_docs(parse_invoice_text(text))
            self.assertEqual([d.day for d in docs], ["2026-05-01"])          # 作廢的不算
            d = docs[0]
            self.assertEqual(d.title, "消費 2 筆｜NT$93")
            self.assertIn("統一超商台南分公司", d.body)
            self.assertIn("折扣｜-40", d.body)
            self.assertIn("飯糰", d.excerpt)

    def test_scan_folder_and_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "a.csv").write_text(COMMA, encoding="utf-8")
            Path(td, "b.csv").write_text(PIPE, encoding="utf-8")       # 重複的發票只算一次
            rows = [{"day": "2026-05-01", "kind": "note", "icon": "📒", "name": "筆記", "body": "今天去爬山"},
                    {"day": "bad", "body": "x"}]
            Path(td, "n.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
            Path(td, "x.bin").write_bytes(b"\0")
            r = scan(Path(td))
            self.assertEqual([(d.kind, d.day) for d in r.docs], [("invoice", "2026-05-01"), ("note", "2026-05-01")])
            self.assertEqual(r.docs[0].title, "消費 2 筆｜NT$93")
            self.assertEqual(r.skipped, ["x.bin"])
            note = r.docs[1]
            self.assertTrue(post_chunks(note)[0].startswith("## 📒 2026-05-01-Friday 筆記"))

    def test_key_and_chunks(self):
        self.assertEqual(doc_key("invoice", "2026-05-01", 0), doc_key("invoice", "2026-05-01", 0))
        self.assertNotEqual(doc_key("invoice", "2026-05-01", 0), doc_key("note", "2026-05-01", 0))
        self.assertEqual(doc_key("x", "2026-05-01", 0)[0], 0)
        from importer.extra import DayDoc
        long = DayDoc("note", "2026-05-01", "筆記", "\n".join(["字" * 100] * 50), "x")
        ch = post_chunks(long)
        self.assertGreater(len(ch), 1)
        self.assertTrue(all(len(c) <= 2000 for c in ch))


if __name__ == "__main__":
    unittest.main()
