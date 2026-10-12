# relay-core/ipc.py
"""App（SwiftUI 選單列）與核心之間的溝通（第 5.3 節）。

- 啟動時環境變數 INBOXRELAY_IPC=1 才啟用。
- App → 核心：stdin 每行一個 JSON：
  {"type": "lane", "lane": "llm|ane|shot", "action": "run_now|pause|resume"}、{"type": "hello", "ocr": true}、
  {"type": "ocr_result", "id": 1, "text": "…", "ms": 120}、{"type": "llm_test", "id": 1, "server", "model", "key"}、{"type": "diag", "on": true|false}、{"type": "stop"}；
  舊指令 recap_now／pause／resume 仍可用（＝外部 LLM 車道）。
- 核心 → App：stdout 每行一個 JSON：狀態（type=status，每 2 秒、有變化才送）、
  文字辨識請求 {"type": "ocr_request", "id": 1, "url": "…", "name": "a.jpg"}、
  LLM 測試結果 {"type": "llm_test_result", "id": 1, "ok": true, "message": "…", "ms": 85}。日誌一律走 stderr。
"""
from __future__ import annotations

import asyncio
import json

import lanes
import logging
import os
import sys

log = logging.getLogger("ipc")


def enabled() -> bool:
    return os.environ.get("INBOXRELAY_IPC") == "1"


def emit(obj: dict) -> None:
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


class IpcServer:
    def __init__(self, act, relayer, get_recapper, tg, cfg, ai_status: str, stop_event: asyncio.Event) -> None:
        self.act = act
        self.relayer = relayer
        self.get_recapper = get_recapper
        self.tg = tg
        self.cfg = cfg
        self.ai_status = ai_status
        self.stop_event = stop_event
        self._last = ""
        self.ocr = None            # ocr.OcrWorker
        self.shot = None           # enrich.shotworker.ShotWorker

    def status(self) -> dict:
        rc = self.get_recapper()
        r = self.relayer
        return {
            "type": "status",
            "running": True,
            "paused": self.act.paused,
            "tasks": [{"label": t.label, "done": t.done, "total": t.total, "detail": t.detail, "line": t.line(),
                       "progress": t.progress}
                      for t in sorted(self.act.tasks.values(), key=lambda t: t.id)],
            "backlog": r.enricher.backlog if r.enricher else 0,
            "probe_pending": r.prober.pending if r.prober else 0,
            "summarized_until": (rc.state.data.get("summarized_until") if rc else "") or "",
            "next_recap_hours": round(rc.seconds_until_start() / 3600, 2) if rc else None,
            "recap_enabled": rc is not None,
            "lanes": self.lanes_status(),
            "recap_phase": rc.phase if rc else "",          # running / queued / ""
            "recap_time": self.cfg.recap_time,
            "chat_title": self.tg.chat_title or "",
            "ai_status": self.ai_status,
            "rss_mb": round(getattr(getattr(self.act, "mem", None), "current", 0) or 0),
        }

    def lanes_status(self) -> dict:
        r = self.relayer
        out = {}
        for name, lane in lanes.LANES.items():
            if name == "llm":
                backlog = r.enricher.backlog if r.enricher else 0
            elif name == "shot":
                backlog = self.shot.backlog if self.shot else 0
            else:
                backlog = self.ocr.backlog if self.ocr else 0
            out[name] = {
                "label": lane.label, "mode": lane.mode, "state": lane.state, "describe": lane.describe(),
                "queued": lane.queued, "backlog": backlog,
                "next_start": lane.next_start.strftime("%m/%d %H:%M") if lane.next_start and lane.state in ("waiting", "offline") else "",
            }
        return out

    async def _llm_test(self, cmd: dict) -> None:
        """設定頁的「測試連線」：測表單目前的值；若測的就是執行中的那組且成功，順便讓離線的外部 LLM 車道恢復。"""
        from llm import test_connection
        server, model, key = str(cmd.get("server") or ""), str(cmd.get("model") or ""), str(cmd.get("key") or "")
        r = await test_connection(server, model, key)
        log.info("🧪 測試外部 LLM（%s／%s）：%s", server, model, r["message"])
        same = (server.rstrip("/") == self.cfg.ai_server.rstrip("/") and model == self.cfg.ai_model)
        if r["ok"] and same:
            self.ai_status = "✅ 已連線"
            lane = lanes.get("llm")
            if lane is not None and lane.offline:
                lane.offline = False
                lane.notify()
                r["message"] += "；外部 LLM 工作已恢復"
        emit({"type": "llm_test_result", "id": cmd.get("id"), **r})
        self._last = ""

    def handle(self, line: str) -> None:
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            return
        t = cmd.get("type")
        rc = self.get_recapper()
        if t == "recap_now" and rc is not None:
            if rc.trigger_now():
                log.info("🧾 App 要求立即總結%s", "（目前暫停中，按「繼續」才會開始）" if self.act.paused else "")
            else:
                log.info("🧾 總結已經在進行／排隊中，忽略重複的「立即總結」")
        elif t == "lane":
            lane = lanes.get(str(cmd.get("lane")))
            action = cmd.get("action")
            if lane is None:
                return
            if action == "pause":
                lane.pause()
            elif action == "resume":
                lane.resume()
            elif action == "run_now":
                if lane.name == "llm" and rc is not None:
                    if not rc.trigger_now():
                        log.info("🧾 外部 LLM 已經在執行／排隊中")
                    else:
                        log.info("🧾 App 要求外部 LLM 立即執行（分析＋總結）")
                elif lane.run_now():
                    log.info("🔤 App 要求%s立即執行", lane.label)
        elif t == "diag":
            import diag
            diag.set_enabled(bool(cmd.get("on")))
            log.info("診斷模式：%s", "開（診斷資料會寫成檔案）" if diag.enabled else "關（只顯示、不寫檔）")
            return
        elif t == "llm_test":
            asyncio.get_running_loop().create_task(self._llm_test(cmd))
            return
        elif t == "recap_reset" and rc is not None:
            log.info("🧹 App 要求清空並重新總結")
            asyncio.get_running_loop().create_task(rc.reset_all())
        elif t == "hello":
            lane = lanes.get("ane")
            if lane is not None and cmd.get("ocr"):
                if not lane.available:
                    log.info("🔤 App 支援本機文字辨識（%s）", cmd.get("device") or "Vision")
                lane.available = True
                lane.notify()
        elif t == "ocr_result":
            if self.ocr is not None:
                self.ocr.on_result(cmd)
            return
        elif t == "pause" and not self.act.paused:
            self.act.pause()
        elif t == "resume" and self.act.paused:
            self.act.resume()
        elif t == "stop":
            log.info("App 要求結束")
            self.stop_event.set()
        self._last = ""   # 讓下一次狀態立刻送出

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

        async def read_loop() -> None:
            while True:
                line = await reader.readline()
                if not line:            # App 關掉了 stdin（App 結束）→ 核心也結束
                    log.info("App 已關閉連線，核心結束")
                    self.stop_event.set()
                    return
                self.handle(line.decode(errors="ignore").strip())

        reader_task = asyncio.create_task(read_loop())
        try:
            while True:
                s = json.dumps(self.status(), ensure_ascii=False)
                if s != self._last:
                    self._last = s
                    sys.stdout.write(s + "\n")
                    sys.stdout.flush()
                await asyncio.sleep(2)
        finally:
            reader_task.cancel()
