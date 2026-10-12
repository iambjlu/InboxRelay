# relay-core/tests/test_startup.py：啟動進度回報（給 App 儀表板顯示）
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ipc  # noqa: E402
import kwindex  # noqa: E402
import startup  # noqa: E402


class StartupTest(unittest.TestCase):
    def test_stage_emits_step_and_total_steps(self):
        sent = []
        with mock.patch.object(ipc, "emit", sent.append):
            startup.stage("載入 #關鍵字庫", "已讀 1,500 則", done=1500)
            startup.stage("不存在的步驟")
        self.assertEqual(sent[0], {"type": "startup", "stage": "載入 #關鍵字庫", "detail": "已讀 1,500 則",
                                   "step": startup.STEPS.index("載入 #關鍵字庫") + 1, "steps": len(startup.STEPS),
                                   "done": 1500, "total": None})
        self.assertEqual(sent[1]["step"], 0)                       # 不認得的階段：step=0（App 不顯示步驟數）

    def test_stage_names_used_by_main_exist(self):
        src = (Path(__file__).resolve().parent.parent / "main.py").read_text() + (Path(__file__).resolve().parent.parent / "relay.py").read_text()
        import re
        for name in re.findall(r'startup\.stage\("([^"]+)"', src):
            self.assertIn(name, startup.STEPS, name)

    def test_kwindex_load_reports_progress_every_500(self):
        async def go():
            class Ch:
                async def _hist(self):
                    for i in range(1200):
                        yield N(author=N(id=2), content="x", id=i + 1)

                def history(self, **k):
                    return self._hist()
            idx = kwindex.KwIndex(Ch(), 1)
            seen = []
            idx.on_progress = seen.append
            await idx.load()
            self.assertEqual(seen, [500, 1000])
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
