# relay-core/statusbar.py
"""終端機底部常駐狀態列（不管視窗怎麼拉都黏在最下面）。

做法：用 ANSI 捲動區域（DECSTBM）把最下面 3 行保留給狀態列，日誌只在上面捲動；
視窗大小改變（SIGWINCH）時重新設定並重畫。不是互動終端機（例如輸出被導到檔案）就自動停用。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import unicodedata
from typing import Callable

ESC = "\x1b"
HEIGHT = 3          # 分隔線＋狀態＋按鍵說明
HR = "<hr>"


def char_width(ch: str) -> int:
    if unicodedata.combining(ch) or ch in ("‍", "️", "︎"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    if ord(ch) >= 0x1F300:          # 大部分 emoji
        return 2
    return 1


def fit(text: str, cols: int) -> str:
    """依顯示寬度截斷（中文、emoji 算 2 格），避免換行把狀態列擠亂。"""
    out, w = [], 0
    limit = max(cols - 1, 1)
    for ch in text:
        cw = char_width(ch)
        if w + cw > limit:
            if out and limit - w >= 1:
                out.append("…")
            break
        out.append(ch)
        w += cw
    return "".join(out)


class StatusBar:
    def __init__(self, provider: Callable[[], list[str]], stream=None) -> None:
        self.provider = provider            # 回傳 HEIGHT 行文字（"<hr>" 代表分隔線）
        self.stream = stream or sys.stderr
        self.enabled = False
        self.rows = self.cols = 0
        self._lock = threading.RLock()
        self._task: asyncio.Task | None = None
        self._orig_emits: list = []

    # ------------------------------------------------------------ 控制
    def start(self) -> bool:
        if os.environ.get("STATUS_BAR", "true").lower() in ("0", "false", "no", "off"):
            return False
        if not (self.stream.isatty() and os.environ.get("TERM", "") not in ("", "dumb")):
            return False
        self.enabled = True
        self._setup(first=True)
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGWINCH, self._on_resize)
        except (NotImplementedError, RuntimeError):
            pass
        # 每印一行日誌就重畫狀態列
        for h in logging.getLogger().handlers:
            orig = h.emit
            self._orig_emits.append((h, orig))

            def emit(record, _orig=orig):
                with self._lock:
                    _orig(record)
                    self.draw()
            h.emit = emit  # type: ignore[method-assign]
        self._task = asyncio.create_task(self._ticker())
        return True

    def stop(self) -> None:
        if not self.enabled:
            return
        self.enabled = False
        if self._task:
            self._task.cancel()
        for h, orig in self._orig_emits:
            h.emit = orig  # type: ignore[method-assign]
        with self._lock:
            # 還原整個畫面為可捲動，清掉狀態列，游標移到最下面
            out = [f"{ESC}[r"]
            for i in range(HEIGHT):
                out.append(f"{ESC}[{self.rows - HEIGHT + 1 + i};1H{ESC}[2K")
            out.append(f"{ESC}[{self.rows - HEIGHT + 1};1H")
            self._write("".join(out))

    # ------------------------------------------------------------ 內部
    def _size(self) -> tuple[int, int]:
        try:
            sz = os.get_terminal_size(self.stream.fileno())
            return sz.lines, sz.columns
        except OSError:
            return 24, 80

    def _write(self, s: str) -> None:
        try:
            self.stream.write(s)
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def _setup(self, first: bool = False) -> None:
        with self._lock:
            self.rows, self.cols = self._size()
            if self.rows < HEIGHT + 4:
                self._write(f"{ESC}[r")       # 視窗太小就先不畫
                return
            top = self.rows - HEIGHT
            out = []
            if first:
                out.append("\n" * HEIGHT)      # 先把現有內容往上推，騰出底部空間
            out.append(f"{ESC}[1;{top}r")      # 捲動區域：第 1 行到倒數第 4 行
            out.append(f"{ESC}[{top};1H")      # 游標放回捲動區域最下面
            self._write("".join(out))
            self.draw()

    def _on_resize(self) -> None:
        if self.enabled:
            self._setup()

    def draw(self) -> None:
        if not self.enabled or self.rows < HEIGHT + 4:
            return
        with self._lock:
            try:
                lines = self.provider()
            except Exception as e:  # 狀態列出錯不能影響主程式
                lines = [HR, f"狀態列錯誤：{e}", ""]
            lines = (lines + [""] * HEIGHT)[:HEIGHT]
            out = [f"{ESC}7"]                                   # 存游標
            for i, ln in enumerate(lines):
                row = self.rows - HEIGHT + 1 + i
                text = "─" * (self.cols - 1) if ln == HR else fit(ln, self.cols)
                style = f"{ESC}[2m" if ln == HR else (f"{ESC}[7m" if i == HEIGHT - 1 else "")
                out.append(f"{ESC}[{row};1H{ESC}[2K{style}{text}{ESC}[0m")
            out.append(f"{ESC}8")                               # 還原游標（回到日誌區）
            self._write("".join(out))

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1)
            if (self.rows, self.cols) != self._size():
                self._setup()
            else:
                self.draw()
