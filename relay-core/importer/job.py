# relay-core/importer/job.py
"""歷史匯入工作（第 9 節）：計數、檢查、搬運、進度、暫停／繼續／取消、檢查點、鎖。

切換流程（第 9.5 節）的 CLI 版：
  匯入時「實時核心不要開」，Telegram 會替 bot 保留未讀訊息 24 小時；
  匯入完再啟動 ./run.sh，已在匯出檔裡的訊息會被 #關鍵字庫 去重擋掉。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator

import discord

import formatting
import records
import state
from config import Config
from dc_output import DiscordOut
from importer.packer import Packet, attachment_content, pack_all
from importer.reader import HistMsg, iter_messages, read_meta

log = logging.getLogger("import")

MIRROR_INTERVAL = 1.2   # 鏡像頻道最短發送間隔（秒）
CHECKPOINT_EVERY = 50
CHECKPOINT_SECONDS = 30
TG_RETENTION_WARN_H = 20


@dataclass
class Plan:
    total: int = 0
    dup: int = 0
    packets: int = 0
    attachments: int = 0
    missing: int = 0
    oversize: int = 0
    min_date: str | None = None
    max_date: str | None = None

    def eta_seconds(self) -> float:
        # 鏡像每則約 1.2 秒；#關鍵字庫 每筆約 1 秒（Discord 單頻道速率）
        return self.packets * MIRROR_INTERVAL + self.total * 1.0


def fmt_dur(sec: float) -> str:
    sec = int(max(sec, 0))
    h, rem = divmod(sec, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def parse_day(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


class ImportJob:
    def __init__(self, cfg: Config, dc: DiscordOut, path: str, date_from: str | None,
                 date_to: str | None, append_out_of_order: bool, dry_run: bool) -> None:
        self.cfg = cfg
        self.dc = dc
        p = Path(path).expanduser()
        self.result = p if p.is_file() else p / "result.json"
        self.base = self.result.parent
        self.d_from = parse_day(date_from)
        self.d_to = parse_day(date_to)
        self.ooo = append_out_of_order
        self.dry = dry_run
        self.seen: set[tuple[int, int]] = set()
        self.chat_id = cfg.tg_inbox_chat_id
        self.limit = 0
        self.paused = asyncio.Event()
        self.paused.set()          # set = 可以跑
        self.cancel = False
        self.done = 0
        self.last_tg_msg: int | None = None
        self.db: state.DbRecord | None = None
        self._last_mirror_send = 0.0

    # ------------------------------------------------------------------ 讀取
    def _in_range(self, m: HistMsg) -> bool:
        d = date.fromisoformat(formatting.local_date(m.dt, self.cfg.tz))
        if self.d_from and d < self.d_from:
            return False
        if self.d_to and d > self.d_to:
            return False
        return True

    def _stream(self) -> Iterator[HistMsg]:
        for m in iter_messages(self.result, self.cfg.tz):
            if self._in_range(m) and (self.chat_id, m.tg_msg) not in self.seen:
                yield m

    def _file_status(self, m: HistMsg) -> tuple[Path | None, str | None]:
        """回傳 (可上傳的路徑, 沒搬時的說明行)。"""
        if m.missing_reason:
            return None, f"📎 {m.file_name}（{m.missing_reason}）"
        fp = self.base / (m.rel_path or "")
        if not fp.is_file():
            return None, f"📎 {m.file_name}（找不到檔案）"
        if fp.stat().st_size > self.limit:
            return None, formatting.oversize_line(m.file_name or fp.name)
        return fp, None

    def build_plan(self, last_header: str | None) -> Plan:
        plan = Plan()
        for m in iter_messages(self.result, self.cfg.tz):
            if not self._in_range(m):
                continue
            if (self.chat_id, m.tg_msg) in self.seen:
                plan.dup += 1
                continue
            plan.total += 1
            d = formatting.local_date(m.dt, self.cfg.tz)
            plan.min_date = d if plan.min_date is None or d < plan.min_date else plan.min_date
            plan.max_date = d if plan.max_date is None or d > plan.max_date else plan.max_date
            if m.rel_path or m.missing_reason:
                plan.attachments += 1
                fp, note = self._file_status(m)
                if note and m.missing_reason or (note and "找不到" in note):
                    plan.missing += 1
                elif note:
                    plan.oversize += 1
        start_header = None if self.ooo else last_header
        plan.packets = sum(1 for _ in pack_all(self._stream(), self.cfg.tz, self.cfg.header_style, start_header))
        return plan

    # ------------------------------------------------------------------ 控制
    def _install_controls(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_cancel)
        if not sys.stdin.isatty():
            return None
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)

        def on_key() -> None:
            ch = os.read(fd, 1).decode(errors="ignore").lower()
            if ch == "p" and self.paused.is_set():
                self.paused.clear()
                self._print("\n⏸  已暫停（按 r 繼續、c 取消）")
            elif ch == "r" and not self.paused.is_set():
                self.paused.set()
                self._print("\n▶️  繼續")
            elif ch in ("c", "q"):
                self._request_cancel()

        loop.add_reader(fd, on_key)

        def restore() -> None:
            loop.remove_reader(fd)
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return restore

    def _request_cancel(self) -> None:
        if not self.cancel:
            self.cancel = True
            self.paused.set()
            self._print("\n⏹  取消中：處理完目前這則就停下並寫入檢查點…")

    @staticmethod
    def _print(s: str) -> None:
        sys.stderr.write(s + "\n")
        sys.stderr.flush()

    def _progress(self, total: int, cur_date: str, started: float) -> None:
        width = 20
        frac = self.done / total if total else 1
        bar = "#" * int(frac * width) + "-" * (width - int(frac * width))
        elapsed = time.monotonic() - started
        eta = elapsed / self.done * (total - self.done) if self.done else 0
        sys.stderr.write(f"\r[{bar}] {self.done:,} / {total:,}｜{cur_date}｜剩餘約 {fmt_dur(eta)}   ")
        sys.stderr.flush()

    # ------------------------------------------------------------------ 檢查點
    async def _checkpoint(self, status: str, plan: Plan) -> None:
        if self.db is None:
            return
        data = {
            "status": status,
            "owner": "mac",
            "heartbeat": state.now_iso(),
            "source": self.base.name,
            "range": f"{self.d_from or '最早'} ~ {self.d_to or '最新'}",
            "chat_id": str(self.chat_id),
            "cursor": str(self.last_tg_msg or ""),
            "done": str(self.done),
            "total": str(plan.total),
            "live_paused": "yes" if status == "running" else "no",
        }
        try:
            await self.db.save(data)
        except discord.HTTPException as e:
            log.warning("寫入檢查點失敗：%s", e)

    # ------------------------------------------------------------------ 發送
    async def _pace(self) -> None:
        wait = self._last_mirror_send + MIRROR_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_mirror_send = time.monotonic()

    async def _send_packet(self, pkt: Packet) -> str:
        """發出一個封包，回傳鏡像訊息連結。"""
        ch = self.cfg.ch_inbox_mirror
        await self._pace()
        if pkt.attachment is None:
            msg = await self.dc.send(ch, pkt.content)
            return msg.jump_url
        m = pkt.attachment
        fp, note = self._file_status(m)
        files: list[tuple[str, bytes]] = []
        if fp is not None:
            files.append((m.file_name or fp.name, fp.read_bytes()))  # 發送前才讀進記憶體
        content = attachment_content(pkt, note, self.cfg.header_style)
        try:
            sent = await self.dc.send_chunks(ch, content, files, self.limit)
        except discord.HTTPException as e:
            if e.status != 413 or not files:
                raise
            note = formatting.oversize_line(files[0][0])
            files.clear()
            content = attachment_content(pkt, note, self.cfg.header_style)
            sent = await self.dc.send_chunks(ch, content, files, self.limit)
        files.clear()
        return sent[0].jump_url

    async def _write_records(self, pkt: Packet, mirror: str) -> None:
        for m in pkt.msgs:
            rec = records.KwRecord(
                tg_chat=self.chat_id,
                tg_msg=m.tg_msg,
                origin="import",
                date=formatting.local_minute(m.dt, self.cfg.tz),
                type=m.type,
                mirror=mirror,
                filename=m.file_name if (m.rel_path or m.missing_reason) else None,
                excerpt=records.make_excerpt(m.text) if m.text else None,
            )
            await self.dc.send(self.cfg.ch_keywords, records.format_kw(rec))
            self.seen.add(rec.key)
            self.done += 1
            self.last_tg_msg = m.tg_msg

    # ------------------------------------------------------------------ 主流程
    async def run(self) -> int:
        if not self.result.is_file():
            self._print(f"❌ 找不到 {self.result}")
            return 2
        meta = read_meta(self.result)
        self._print(f"📦 匯出檔：{meta.name}（{meta.type}），換算 chat_id = {meta.bot_chat_id}")
        if meta.bot_chat_id != self.cfg.tg_inbox_chat_id:
            self._print(f"❌ 匯出檔的群組（{meta.bot_chat_id}）和 .env 的 TG_INBOX_CHAT_ID"
                        f"（{self.cfg.tg_inbox_chat_id}）不同。請確認匯出的是投遞箱，或修改 .env。")
            return 2

        self.limit = self.dc.filesize_limit(self.cfg.upload_limit_mb)
        self._print("🔎 讀取 #關鍵字庫（去重用）與 #投遞箱鏡像 最後的日期…")
        self.seen = await self.dc.load_kw_keys(self.cfg.ch_keywords)
        last_header = await self.dc.last_header_date(self.cfg.ch_inbox_mirror)

        self._print("🔢 掃描匯出檔、計算數量…")
        plan = self.build_plan(last_header)
        eta = plan.eta_seconds()
        self._print(
            f"   範圍內新訊息 {plan.total:,} 則（已搬過略過 {plan.dup:,}），日期 {plan.min_date} ~ {plan.max_date}\n"
            f"   附件 {plan.attachments:,}（過大只貼檔名 {plan.oversize:,}、未匯出／找不到 {plan.missing:,}）\n"
            f"   預計發出鏡像 {plan.packets:,} 則＋關鍵字紀錄 {plan.total:,} 筆，約需 {fmt_dur(eta)}\n"
            f"   頻道目前最後的日期：{last_header or '（空）'}；檔案上限 {self.limit / 1024 / 1024:.0f} MB")
        if plan.total == 0:
            self._print("✅ 沒有需要匯入的訊息")
            return 0

        out_of_order = bool(last_header and plan.min_date and plan.min_date < last_header)
        if out_of_order and not self.ooo:
            self._print(f"❌ 要匯入的最早日期 {plan.min_date} 早於頻道裡已有的 {last_header}，"
                        "照順序只能加在最後面、時間軸會倒退。\n"
                        "   若是第一次匯入：請先清空 #投遞箱鏡像 與 #關鍵字庫 的測試訊息。\n"
                        "   若確定要補匯入：加上 --append-out-of-order。")
            return 2
        if eta > TG_RETENTION_WARN_H * 3600:
            self._print(f"⚠️  預估超過 {TG_RETENTION_WARN_H} 小時；Telegram 只保留未讀訊息 24 小時，"
                        "建議用 --from/--to 分段，每段匯完就開一次 ./run.sh 讓實時補上。")

        if self.dry:
            self._print("🧪 --dry-run：只檢查，不發任何訊息。")
            return 0

        # ---- 鎖 ----
        db_ch = await self.dc.channel(self.cfg.ch_db)
        self.db = state.DbRecord(db_ch, self.dc.client.user.id, "IMPORT")  # type: ignore[union-attr]
        current = await self.db.load()
        if state.import_lock_active(current):
            self._print(f"❌ 另一個匯入正在進行（owner={current.get('owner')}，"
                        f"heartbeat={current.get('heartbeat')}）。5 分鐘沒更新才會視為中斷。")
            self.db = None
            return 3

        self._print("⚠️  匯入期間請不要開 ./run.sh（實時核心）。按 p 暫停、r 繼續、c 取消。\n")
        restore = self._install_controls()
        status = "running"
        await self._checkpoint(status, plan)
        started = time.monotonic()
        last_cp, last_cp_done = time.monotonic(), 0
        try:
            if self.ooo and out_of_order:
                await self._pace()
                await self.dc.send(self.cfg.ch_inbox_mirror,
                                   f"-# ⤴ 以下為補匯入的舊資料（{plan.min_date} ~ {plan.max_date}）")
            start_header = None if (self.ooo and out_of_order) else last_header
            for pkt in pack_all(self._stream(), self.cfg.tz, self.cfg.header_style, start_header):
                await self.paused.wait()
                if self.cancel:
                    status = "cancelled"
                    break
                mirror = await self._send_packet(pkt)
                await self._write_records(pkt, mirror)
                self._progress(plan.total, pkt.date, started)
                if (self.done - last_cp_done >= CHECKPOINT_EVERY
                        or time.monotonic() - last_cp >= CHECKPOINT_SECONDS):
                    await self._checkpoint("running", plan)
                    last_cp, last_cp_done = time.monotonic(), self.done
            else:
                status = "done"
            if status == "done" and self.ooo and out_of_order:
                await self._pace()
                await self.dc.send(self.cfg.ch_inbox_mirror, "-# ⤵ 補匯入結束")
        except Exception:
            status = "failed"
            raise
        finally:
            if restore:
                restore()
            await self._checkpoint(status, plan)
            self._print("")
            if status == "done":
                self._print(f"✅ 匯入完成：{self.done:,} 則，用時 {fmt_dur(time.monotonic() - started)}。"
                            "現在可以啟動 ./run.sh 恢復實時搬移。")
            elif status == "cancelled":
                self._print(f"⏹  已取消：完成 {self.done:,} / {plan.total:,}。"
                            "用同一行指令再跑一次就會從中斷處接著做（已搬過的自動略過）。")
            else:
                self._print(f"❌ 匯入中斷（完成 {self.done:,} 則）。修正問題後用同一行指令重跑即可續傳。")
        return 0 if status == "done" else 1
