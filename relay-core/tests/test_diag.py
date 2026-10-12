# relay-core/tests/test_diag.py：診斷模式——預設關閉只顯示不寫檔，打開才寫檔
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import diag  # noqa: E402
import ipc  # noqa: E402
import main  # noqa: E402


class DiagTest(unittest.TestCase):
    def setUp(self):
        diag.set_enabled(False)

    def tearDown(self):
        diag.set_enabled(False)

    def test_default_off_and_ipc_toggles(self):
        self.assertFalse(diag.enabled)
        srv = ipc.IpcServer.__new__(ipc.IpcServer)
        srv.get_recapper = lambda: None
        srv.act = N(paused=False)
        srv._last = "x"
        srv.handle('{"type": "diag", "on": true}')
        self.assertTrue(diag.enabled)
        srv.handle('{"type": "diag", "on": false}')
        self.assertFalse(diag.enabled)

    def test_dump_tasks_writes_file_only_when_enabled(self):
        async def go(on):
            diag.set_enabled(on)
            with tempfile.TemporaryDirectory() as d, mock.patch("paths.data_dir", lambda: Path(d)), \
                    self.assertLogs("__main__" if main.log.name == "__main__" else main.log.name, level="INFO") as cm:
                main.dump_tasks()
                return (Path(d) / "tasks-dump.txt").exists(), "\n".join(cm.output)
        wrote, logged = asyncio.run(go(False))
        self.assertFalse(wrote)                                   # 關閉：不寫檔
        self.assertIn("診斷｜", logged)                            # 但內容印在日誌（App 日誌分頁看得到）
        wrote, logged = asyncio.run(go(True))
        self.assertTrue(wrote)                                    # 打開：寫檔
        self.assertIn("tasks-dump.txt", logged)


if __name__ == "__main__":
    unittest.main()
