# relay-core/startup.py
"""啟動進度回報：核心啟動要花幾分鐘（尤其是讀 #關鍵字庫），每個階段主動告訴 App，儀表板就能直接顯示現在做到哪，
不用看日誌。只在 App 管理（INBOXRELAY_IPC=1）時才送；終端機模式不送。

App 收到 {"type":"startup","stage":…,"detail":…,"step":N,"steps":M,"done":數量|null,"total":數量|null}。
第一筆一般狀態列（type=status）出現就代表啟動完成，App 會把啟動進度收起來。
"""
from __future__ import annotations

import ipc

STEPS = [
    "連線 Discord",
    "檢查頻道與權限",
    "連線外部 LLM",
    "載入記憶與提示詞",
    "連線 Telegram",
    "載入 #關鍵字庫",
    "載入 #總結與狀態",
    "啟動各項工作",
]


def stage(name: str, detail: str = "", done: int | None = None, total: int | None = None) -> None:
    step = STEPS.index(name) + 1 if name in STEPS else 0
    ipc.emit({"type": "startup", "stage": name, "detail": detail, "step": step, "steps": len(STEPS),
              "done": done, "total": total})

