# relay-core/diag.py
"""診斷模式開關。預設關閉：日誌只顯示（App 的「日誌」分頁、終端機），完全不寫檔。
打開後（App 設定頁「診斷模式」，經 IPC {"type":"diag","on":true} 即時通知核心）才會把診斷資料寫成檔案
（目前是 kill -USR1 的任務快照 tasks-dump.txt；日誌檔由 App 負責寫）。"""
from __future__ import annotations

enabled = False


def set_enabled(on: bool) -> None:
    global enabled
    enabled = bool(on)
