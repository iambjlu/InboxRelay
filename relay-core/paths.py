# relay-core/paths.py
"""可寫入的位置：App 版把核心放在 InboxRelay.app 裡（唯讀），會寫的東西一律放
~/Library/Application Support/InboxRelay（或環境變數 INBOXRELAY_DATA 指定的地方）。"""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    p = Path(os.environ.get("INBOXRELAY_DATA") or (Path.home() / "Library" / "Application Support" / "InboxRelay"))
    p.mkdir(parents=True, exist_ok=True)
    return p
