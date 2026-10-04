# relay-core/main.py
"""InboxRelay 核心進入點。

用法：
  python main.py                 實時搬移（M1＋M2）
  python main.py --check         只檢查 Token、群組與頻道權限，不發任何訊息
  python main.py import --path <匯出資料夾> [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                 [--dry-run] [--append-out-of-order]
                                 歷史匯入（M6，CLI 版）
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import signal
import sys
import warnings
from pathlib import Path

# macOS 內建 Python 3.9 用 LibreSSL，urllib3 v2 會一直跳警告；不影響功能
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

import discord

import activity
import config
import lanes
import state
from dc_output import DiscordOut
from llm import LLM
from relay import Relayer
from tg_intake import RelayItem, TelegramIntake

log = logging.getLogger("main")


class _TelegramNoiseFilter(logging.Filter):
    """把 PTB 輪詢的網路錯誤從整段 traceback 縮成一行。

    - 偶發的連線中斷（ReadError、TimedOut）PTB 會自己重試 → 一行 WARNING。
    - Conflict 代表同一個 Bot Token 有兩個程式在輪詢 → 一行 ERROR，講清楚原因。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if exc is None:
            return True
        from telegram.error import Conflict, NetworkError
        if isinstance(exc, Conflict):
            record.msg = ("Telegram 回報 Conflict：同一個 Bot Token 有另一個程式也在收訊息"
                          "（是不是開了兩個 ./run.sh？或別的程式用了同一個 Token）")
        elif isinstance(exc, NetworkError):
            record.msg = f"Telegram 連線暫時中斷（{exc}），會自動重試"
            record.levelno, record.levelname = logging.WARNING, "WARNING"
        else:
            return True
        record.args, record.exc_info, record.exc_text = (), None, None
        return True


_LOCK_HANDLE = None


def single_instance() -> bool:
    """檔案鎖（~/Library/Application Support/InboxRelay/instance.lock），避免同時開兩個（App 與終端機共用同一把鎖）。"""
    global _LOCK_HANDLE
    from paths import data_dir
    _LOCK_HANDLE = open(data_dir() / "instance.lock", "a+")
    try:
        fcntl.flock(_LOCK_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def setup_logging() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx 的 INFO 日誌會印出含 Bot Token 的網址，一律壓到 WARNING
    for noisy in ("httpx", "httpcore", "telegram.ext", "discord.gateway", "discord.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Updater").addFilter(_TelegramNoiseFilter())


HELP_KEYS = "按鍵：n 立即執行（分析＋總結）｜p 暫停背景 AI｜r 繼續｜s 狀態｜h 說明｜Ctrl+C 結束"


def install_console(act, relayer, get_recapper):
    """終端機按鍵控制（不用按 Enter）。回傳還原終端機設定的函式。"""
    import os
    if not sys.stdin.isatty():
        return None
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    loop = asyncio.get_running_loop()

    def on_key() -> None:
        ch = os.read(fd, 1).decode(errors="ignore").lower()
        rc = get_recapper()
        if ch == "n":
            if rc is None:
                log.info("每日總結沒有啟用（需要外部 LLM）")
            else:
                if rc.trigger_now():
                    log.info("🧾 立即總結：開始處理所有積欠的每日／月總結%s",
                             "（目前暫停中，按 r 才會開始）" if act.paused else "")
                else:
                    log.info("🧾 總結已經在進行／排隊中，不用再按")
        elif ch == "p":
            if act.paused:
                log.info("已經是暫停狀態（按 r 繼續）")
            else:
                act.pause()
        elif ch == "r":
            if act.paused:
                act.resume()
            else:
                log.info("沒有暫停，不用繼續")
        elif ch == "s":
            lines = act.status_lines() or ["目前沒有背景工作"]
            for ln in lines:
                log.info("📊 %s", ln)
            if relayer.enricher is not None:
                log.info("📊 待分析佇列：%d 則", relayer.enricher.backlog)
            if relayer.prober is not None:
                log.info("📊 待刪除同步確認：%d 則", relayer.prober.pending)
            if rc is not None:
                log.info("📊 每日總結已完成到：%s｜下次自動總結約 %.1f 小時後",
                         rc.state.data.get("summarized_until") or "尚無", rc.seconds_until_start() / 3600)
        elif ch in ("h", "?"):
            log.info(HELP_KEYS)

    loop.add_reader(fd, on_key)

    def restore() -> None:
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return restore


async def run(check_only: bool) -> int:
    cfg = config.load()
    import enrich.screenshot as _shot
    _shot.ALLOW_PRIVATE = cfg.private_urls
    _shot.IGNORE_CERT = cfg.ignore_cert
    act = activity.init()
    lanes.setup(cfg)                       # 外部 LLM／本機 ANE 兩條車道的排程
    restore_console = None
    llm = LLM(cfg.ai_server, cfg.ai_model, cfg.ai_key, cfg.ai_image_support) \
        if cfg.ai_server and cfg.ai_model else None
    # 有 AI 時要讀 #資料庫 裡使用者手動發的 #記憶／#提示詞，所以需要 Message Content Intent
    # 反向同步要讀使用者在 #投遞箱鏡像 發的內容，也需要 Message Content Intent
    dc = DiscordOut(cfg.dc_token, cfg.dc_guild_id, read_messages=bool(llm) or cfg.reverse_sync)
    aitest = None
    queue: "asyncio.Queue[RelayItem]" = asyncio.Queue()
    tg = TelegramIntake(cfg.tg_token, cfg.tg_inbox_chat_id, cfg.tz, cfg.src_link, queue)
    relayer = Relayer(cfg, dc, tg.bot, queue)
    worker: asyncio.Task | None = None
    background: list[asyncio.Task] = []
    mem = None
    ai_status = "未設定"
    recapper = None

    try:
        try:
            await dc.start()
        except discord.PrivilegedIntentsRequired:
            log.error("AI 功能需要 Message Content Intent：請到 Developer Portal → Bot 開啟後重試")
            return 2
        chans = cfg.channel_map()
        problems = await dc.check_channels(chans)
        for p in problems:
            log.error("頻道檢查：%s", p)
        if problems:
            return 2
        # 選用頻道（實驗、搜尋測試、連結預覽、通知）不存在或沒權限 → 只停用那個功能，不影響主流程
        import dataclasses
        for label, attr in (("AI 測試", "ch_ai_test"), ("搜尋測試", "ch_search_test"),
                            ("連結預覽與進度", "ch_screenshot"), ("通知與互動", "ch_notify"),
                            ("AI 對話", "ch_chat"), ("AI 對話提示詞", "ch_chat_prompt")):
            cid = getattr(cfg, attr)
            if cid:
                bad = await dc.check_channels({label: cid})
                if bad:
                    log.warning("%s → 這個選用功能先停用（頻道刪了的話，記得把設定清空）", bad[0])
                    cfg = dataclasses.replace(cfg, **{attr: None})
                else:
                    chans[label] = cid
        relayer.cfg = cfg
        chat_channel = None
        if llm and cfg.ch_chat and cfg.ch_chat_prompt:
            # AI 對話只需要 Discord 與外部 LLM，不用等 #關鍵字庫 等資料載完：登入完成就啟用（含按鈕與收訊息）
            from chat import ChatChannel
            guild0 = dc.guild
            chat_channel = ChatChannel(
                cfg, dc, llm, set(cfg.memory_editors) | ({guild0.owner_id} if guild0 and guild0.owner_id else set()),
                await dc.channel(cfg.ch_notify) if cfg.ch_notify else None)
            chat_channel.register_views()
            try:
                await chat_channel.setup()
            except Exception:
                log.exception("AI 對話啟用失敗（其他功能照常）")
        log.info("%d 個頻道權限檢查通過，檔案上限 %.1f MB",
                 len(chans), dc.filesize_limit(cfg.upload_limit_mb) / 1024 / 1024)
        use_test = bool(cfg.ch_ai_test and llm)
        if use_test:
            from aitest import AiTestChannel
            aitest = AiTestChannel(cfg, dc, llm)  # type: ignore[arg-type]

        if llm:
            try:
                models = await asyncio.wait_for(llm.ping(), timeout=15)
                ok = cfg.ai_model in models
                ai_status = "✅ 已連線" if ok else "⚠️ 已連線但找不到模型"
                log.info("外部 LLM 已連線：%s（模型 %s %s）", cfg.ai_server, cfg.ai_model,
                         "✅" if ok else f"❌ 不在清單：{', '.join(models[:5])}")
            except Exception as e:
                ai_status = "❌ 連不上"
                log.warning("外部 LLM 連不上（%s）：%s；搬移照常，AI 工作留到下次開始時間再試", cfg.ai_server, e)
                lane = lanes.get("llm")
                if lane is not None:
                    lane.go_offline(type(e).__name__)
        if llm:
            from memory import MemoryStore
            guild = dc.guild
            editors = set(cfg.memory_editors) | ({guild.owner_id} if guild and guild.owner_id else set())
            mem = MemoryStore(
                llm=llm,
                db_channel=await dc.channel(cfg.ch_db),
                notify_channel=await dc.channel(cfg.ch_notify) if cfg.ch_notify else None,
                bot_id=dc.client.user.id,  # type: ignore[union-attr]
                editors=editors,
                tz=cfg.tz,
            )
            from memory import PromptStore
            pstore = PromptStore(mem.db_channel, mem.notify_channel, mem.bot_id, editors, cfg.tz)
            await pstore.load(publish_defaults=not check_only)
            await mem.load()
            if not check_only:
                asyncio.create_task(mem.refresh_once(
                    "memory-rules-v2", "待確認只留反覆出現、看不懂就會整理錯的縮寫或代號，最多 3 條，沒有就寫「無」；不要放專案或事件的後續、人際關係、一次性的生活事件、只是看看的東西、電量金額重量等數字紀錄。"))
            if aitest:
                aitest.memory = mem
                aitest.prompts = pstore
        if use_test:
            log.info("AI 測試頻道已啟用：%s", cfg.ch_ai_test)

        await tg.initialize()
        me = await tg.bot.get_me()
        log.info("Telegram bot：@%s（可讀所有群組訊息：%s）", me.username, me.can_read_all_group_messages)

        await relayer.load_state()

        # 同一時間只允許一台電腦在跑（設定同步到多台 Mac 時避免互搶 Telegram 訊息）
        live = state.DbRecord(await dc.channel(cfg.ch_db), dc.client.user.id, "LIVE")  # type: ignore[union-attr]
        other = state.other_host_active(await live.load(), state.this_host())
        if other and not check_only:
            log.error("另一台電腦「%s」正在運行 InboxRelay，這台先不啟動（它停掉 3 分鐘後就能接手）", other)
            return 5

        # 匯入進行中就不開實時，避免時間軸交錯（第 9.5 節）
        db = state.DbRecord(await dc.channel(cfg.ch_db), dc.client.user.id, "IMPORT")  # type: ignore[union-attr]
        imp = await db.load()
        if state.import_lock_active(imp):
            log.error("匯入正在進行（owner=%s，已完成 %s / %s），實時搬移先不啟動。匯入結束後再執行 ./run.sh",
                      imp.get("owner"), imp.get("done"), imp.get("total"))
            return 3

        # ---- M4：刪除同步、AI 分析、需重新總結的日期 ----
        relayer.dirty = state.DirtyDays(state.DbRecord(await dc.channel(cfg.ch_db), dc.client.user.id,  # type: ignore[union-attr]
                                                       "RECAP_DIRTY"))
        await relayer.dirty.load()
        if cfg.delete_sync and cfg.tg_probe_chat_id:
            from probe import Prober
            relayer.prober = Prober(tg.bot, cfg.tg_probe_chat_id, cfg.probe_delay_sec,
                                    relayer.on_alive, relayer.on_deleted)
        elif cfg.delete_sync:
            log.warning("沒有設定 TG_PROBE_CHAT_ID，刪除同步停用")
        if llm:
            from enrich.pipeline import Enricher
            relayer.enricher = Enricher(cfg, dc, llm, relayer.index, relayer.text_cache, mem)  # type: ignore[arg-type]
        if cfg.link_screenshot and not shot_ok():
            log.warning("LINK_SCREENSHOT=true 但找不到 tools/linkshot（需要 Xcode 命令列工具，run.sh 會自動編譯）")
        log.info("功能：刪除同步 %s、編輯同步 %s（%g 小時內）、AI 分析 %s",
                 "開" if relayer.prober else "關", "開" if cfg.edit_sync else "關",
                 cfg.edit_sync_hours, "開" if relayer.enricher else "關")

        if check_only:
            log.info("檢查完成 ✅")
            return 0

        dc.add_delete_handler(relayer.on_discord_delete)
        if cfg.reverse_sync:
            from dc_reverse import ReverseSync
            relayer.reverse = ReverseSync(relayer)
            dc.add_message_handler(relayer.reverse.on_message)
            dc.add_edit_handler(relayer.reverse.on_edit)
            log.info("反向同步已啟用：在 #投遞箱鏡像 發的訊息會同步到 Telegram（含編輯、刪除）")
        # AI 進度訊息放在 #投遞箱鏡像 最下面（有新進度就發新的、刪舊的）；舊版放的頻道也一併清掉殘留
        progress_channel = await dc.channel(cfg.ch_inbox_mirror)
        for cid in {cfg.ch_inbox_mirror, cfg.ch_screenshot, cfg.ch_keywords} - {None}:
            await act.cleanup_old(await dc.channel(cid), dc.client.user.id)  # type: ignore[union-attr]
        background.append(asyncio.create_task(act.reporter(progress_channel)))
        if relayer.enricher:
            background.append(asyncio.create_task(relayer.enricher.run()))
        if llm and cfg.recap:
            from recap.runner import RecapChannel, Recapper
            rc = RecapChannel(await dc.channel(cfg.ch_recap), dc.client.user.id)  # type: ignore[arg-type,union-attr]
            await rc.load()
            st = state.DbRecord(await dc.channel(cfg.ch_db), dc.client.user.id, "STATE")  # type: ignore[union-attr]
            await st.load()
            await st.merge(chat_id=str(tg.chat_id), group_type=str(tg.chat_type))
            notes = state.DbRecord(await dc.channel(cfg.ch_db), dc.client.user.id, "RECAP_NOTES")  # type: ignore[union-attr]
            await notes.load()
            recapper = Recapper(cfg, llm, relayer.index, rc, st, relayer.dirty, relayer.enricher,
                                cfg.recap_wait_hours, notes)

            async def _recap_deleted(channel_id: int, message_id: int, cached) -> None:
                if channel_id == cfg.ch_recap and rc.forget_message(message_id):
                    log.info("#總結 有一篇被手動刪除，下次檢查會重新產生")
            dc.add_delete_handler(_recap_deleted)
            recapper.set_schedule(cfg.recap_time)
            if st.data.get("recap_layout") != "2":
                # 新版 #總結 版面（依日期佔位、原地更新）：第一次啟動時清掉舊的總結、重建目錄（只做一次）
                async def _migrate_recap() -> None:
                    try:
                        await recapper.reset_all()
                        await st.merge(recap_layout="2")
                    except Exception:
                        log.exception("重建 #總結 失敗（下次啟動會再試）")
                background.append(asyncio.create_task(_migrate_recap()))
            log.info("每日／月／年總結已啟用（已完成到 %s）", st.data.get("summarized_until") or "尚無")
        else:
            recapper, rc = None, None
        llm_lane = lanes.get("llm")
        enr = relayer.enricher
        background.append(asyncio.create_task(lanes.run_lane(
            llm_lane, drained=lambda: enr is None or enr.drained,
            cycle=recapper.cycle if recapper is not None else None)))
        log.info("🚦 外部 LLM 工作：%s；本機 ANE 文字辨識：%s（搜尋、實驗頻道等互動隨時可用）",
                 llm_lane.describe(), lanes.get("ane").describe())
        if llm:
            guild = dc.guild
            editors = set(cfg.memory_editors) | ({guild.owner_id} if guild and guild.owner_id else set())
            live_ids = {cfg.ch_search} if cfg.search_live else set()
            test_ids = {cfg.ch_search_test} if cfg.ch_search_test else set()
            if live_ids or test_ids:
                from search.channel import SearchChannel
                SearchChannel(cfg, dc, llm, relayer.index, rc, live_ids, test_ids, aitest)
                log.info("搜尋已啟用：正式 #搜尋 %s；測試頻道 %s（搜 AI 測試頻道的內容）",
                         "開" if live_ids else "關（SEARCH_LIVE=false）", "開" if test_ids else "關")
            if chat_channel is None and cfg.ch_chat:
                log.warning("AI 對話需要同時設定 DC_CH_CHAT 與 DC_CH_CHAT_PROMPT（提示詞與記憶頻道）")
            if cfg.ch_notify:
                from feedback import FeedbackChannel
                FeedbackChannel(cfg, dc, llm, recapper, mem, editors, cfg.ch_notify)
                log.info("通知頻道互動已啟用：%s", cfg.ch_notify)
        queue_backlog(cfg, relayer)
        background.append(asyncio.create_task(poll_new_records(relayer)))

        async def heartbeat() -> None:
            while True:
                try:
                    await live.save({"status": "running", "host": state.this_host(), "heartbeat": state.now_iso()})
                except Exception as e:
                    log.warning("更新運行心跳失敗：%s", e)
                await asyncio.sleep(60)
        background.append(asyncio.create_task(heartbeat()))

        worker = asyncio.create_task(relayer.run())
        await tg.start_polling()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        log.info("InboxRelay 運行中｜%s", HELP_KEYS)
        import ipc
        from memguard import MemGuard
        guard = MemGuard(
            cfg.memory_soft_mb, cfg.memory_hard_mb,
            trims=[relayer.text_cache.clear] + ([aitest.cache.clear] if aitest else []),
            notify=lambda text: notify(dc, cfg, text),
            managed_by_app=ipc.enabled(), stop_event=stop)
        act.mem = guard
        background.append(asyncio.create_task(guard.loop()))
        ane_lane = lanes.get("ane")
        shot_worker = None
        if cfg.link_screenshot and cfg.ch_screenshot and shot_ok():
            from enrich.shotworker import ShotWorker
            shot_worker = ShotWorker(cfg, dc, relayer.index, relayer.text_cache)
            if relayer.enricher is not None:
                relayer.enricher.shot_worker = shot_worker
            shot_lane = lanes.get("shot")
            shot_worker.scan_index()
            background.append(asyncio.create_task(shot_worker.run()))
            async def _shot_maintenance() -> None:                  # 一次性工作：舊失敗重試 → 舊鏡像補截圖箭頭
                await shot_worker.retry_old_failures()
                await shot_worker.backfill_arrows()
            background.append(asyncio.create_task(_shot_maintenance()))
            background.append(asyncio.create_task(lanes.run_lane(
                shot_lane, drained=lambda: shot_worker.drained, cycle=shot_worker.cycle)))
            if cfg.llm_wait_shot:
                llm_l = lanes.get("llm")
                llm_l.blocked_by = lambda: (shot_lane.state == "running" and not shot_worker.drained)
                shot_worker.on_drained = llm_l.notify          # 截圖做完 → 叫醒外部 LLM 車道
            log.info("📸 連結截圖：%s%s", shot_lane.describe(), "（外部 LLM 等截圖做完才開始）" if cfg.llm_wait_shot else "")
        else:
            lanes.LANES.pop("shot", None)           # 沒開連結截圖：不顯示這條車道
        if ipc.enabled():
            from ocr import OcrWorker
            server = ipc.IpcServer(act, relayer, lambda: recapper, tg, cfg, ai_status, stop)
            relayer.ocr = OcrWorker(dc, relayer.index, ipc.emit)
            server.ocr = relayer.ocr
            server.shot = shot_worker
            if relayer.enricher is not None:
                relayer.enricher.on_link_images = relayer.ocr.submit
            if recapper is not None:
                relayer.ocr.has_daily = lambda day: recapper.rc.has("DAILY", day)
                relayer.ocr.mark_days = relayer.dirty.mark_many
            ane_lane.available = False            # 等 App 回報支援文字辨識（hello）
            relayer.ocr.scan_index()
            background.append(asyncio.create_task(relayer.ocr.run()))
            background.append(asyncio.create_task(lanes.run_lane(ane_lane, drained=lambda: relayer.ocr.drained,
                                                             cycle=relayer.ocr.cycle)))
            background.append(asyncio.create_task(server.run()))
            log.info("由 InboxRelay.app 管理（IPC 已啟用）")
        if relayer.enricher is not None:
            async def _refresh_links() -> None:
                from enrich.refresh import refresh_links
                while not relayer.enricher.drained:          # 新訊息的分析先做完
                    await asyncio.sleep(30)
                await refresh_links(dc, llm, relayer.index, cfg,
                                    relayer.ocr.submit if relayer.ocr is not None else None)
            background.append(asyncio.create_task(_refresh_links()))
        else:
            ane_lane.available = False
            log.info("圖片文字辨識需要從 InboxRelay.app 啟動（終端機模式不辨識，App 開著時會自動補做）")
        restore_console = install_console(act, relayer, lambda: recapper) if not ipc.enabled() else None
        from statusbar import StatusBar
        bar = StatusBar(lambda: status_bar_lines(cfg, act, relayer, recapper, tg))
        if bar.start():
            act.terminal_progress = False

            prev_restore = restore_console

            def restore_all() -> None:
                bar.stop()
                if prev_restore:
                    prev_restore()
            restore_console = restore_all
        await notify(dc, cfg, online_message(cfg, tg, relayer, mem, recapper, ai_status))
        await stop.wait()
        log.info("收到結束訊號，關閉中…")
        import os
        import threading
        killer = threading.Timer(25, lambda: os._exit(0))      # 優雅關閉卡住時強制結束，避免殘留行程佔著單一實例鎖
        killer.daemon = True
        killer.start()
        await notify(dc, cfg, f"🔴 **InboxRelay 已離線**（{state.this_host()}，手動結束）\n"
                              "-# 離線期間的 Telegram 訊息會保留 24 小時，下次上線自動補搬")
        try:
            await live.save({"status": "stopped", "host": state.this_host(), "heartbeat": state.now_iso()})
        except Exception:
            pass
        return 0
    finally:
        if restore_console:
            restore_console()
        await act.close()
        await tg.stop()  # 會先送出緩衝中的相簿
        if relayer.prober:
            await relayer.prober.stop()
        for t in background:
            t.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        if worker:
            try:
                await asyncio.wait_for(queue.join(), timeout=60)
            except asyncio.TimeoutError:
                log.warning("仍有 %d 筆未搬完就關閉", queue.qsize())
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await dc.close()


async def notify(dc: DiscordOut, cfg, text: str) -> None:
    """發到「通知與互動」頻道；失敗不影響主流程。"""
    if not cfg.ch_notify:
        return
    try:
        await dc.send(cfg.ch_notify, text, retries=1)
    except Exception as e:
        log.warning("發送通知失敗：%s", e)


def shot_ok() -> bool:
    from enrich.screenshot import available
    return available()


def online_message(cfg, tg, relayer: Relayer, mem, recapper, ai_status: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(cfg.tz)).strftime("%Y-%m-%d %H:%M")
    on, off = "✅", "⛔"
    lines = [
        f"🟢 **InboxRelay 已上線**（{now}｜{__import__('state').this_host()}）",
        f"• 投遞箱：{tg.chat_title}（{'超級群組' if tg.chat_type == 'supergroup' else '一般群組'}）",
        f"• 外部 LLM：{ai_status}" + (f"｜記憶 v{mem.version}" if mem and mem.version else ""),
        f"• 刪除同步 {on if relayer.prober else off}　編輯同步 {on if cfg.edit_sync else off}"
        f"　AI 分析 {on if relayer.enricher else off}　每日總結 {on if recapper else off}",
        f"• 搜尋：#搜尋 {on if cfg.search_live else off}　測試頻道 {on if cfg.ch_search_test else off}"
        f"　連結截圖 {on if (cfg.link_screenshot and shot_ok()) else off}",
    ]
    pending = []
    if relayer.enricher and relayer.enricher.backlog:
        pending.append(f"待分析 {relayer.enricher.backlog} 則")
    if relayer.prober and relayer.prober.pending:
        pending.append(f"待刪除同步確認 {relayer.prober.pending} 則")
    if recapper is not None and recapper.state.data.get("summarized_until"):
        pending.append(f"每日總結到 {recapper.state.data['summarized_until']}")
    if pending:
        lines.append("• " + "｜".join(pending))
    lines.append("-# 在這裡直接打字就能互動，例如「9/30 的總結要補上○○」")
    return "\n".join(lines)


def status_bar_lines(cfg, act, relayer: Relayer, recapper, tg) -> list[str]:
    """底部狀態列三行：分隔線／目前狀態與進度／按鍵說明。"""
    from statusbar import HR
    llm_lane = lanes.get("llm")
    head = "⏸ 背景 AI 已暫停" if act.paused else (
        f"🌙 外部 LLM 等待中（{llm_lane.describe()}）" if llm_lane and llm_lane.state == "waiting" else "🟢 運行中")
    tasks = sorted(act.tasks.values(), key=lambda t: t.id)
    if tasks:
        doing = "⏳ " + tasks[0].line() + (f"（另有 {len(tasks) - 1} 項）" if len(tasks) > 1 else "")
    else:
        doing = "目前沒有背景工作"
    extra = []
    if relayer.enricher is not None and relayer.enricher.backlog:
        extra.append(f"待分析 {relayer.enricher.backlog:,}")
    if relayer.prober is not None and relayer.prober.pending:
        extra.append(f"待確認刪除 {relayer.prober.pending}")
    if recapper is not None:
        extra.append(f"總結到 {recapper.state.data.get('summarized_until') or '—'}")
        h = recapper.seconds_until_start() / 3600
        extra.append(f"下次總結 {cfg.recap_time}（{h:.1f}h 後）")
    mem = getattr(act, "mem", None)
    if mem is not None:
        extra.append(f"記憶體 {mem.current:.0f}MB")
    line2 = "｜".join([head, doing] + extra)
    line3 = f" {HELP_KEYS.replace('按鍵：', '')}　｜　{tg.chat_title or ''}"
    return [HR, line2, line3]


def queue_backlog(cfg, relayer: Relayer) -> None:
    """啟動時把 #關鍵字庫 裡還沒分析的紀錄排進佇列。
    24 小時內的實時訊息先做刪除同步探測；其他（含匯入的）直接分析。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    if relayer.index is None:
        return
    pending: dict[str, list] = {}
    for e in sorted(relayer.index.entries.values(), key=lambda e: e.msg_id):
        if not e.rec.enriched:
            pending.setdefault(e.rec.mirror, []).append(e)
    if not pending:
        return
    cutoff = (datetime.now(ZoneInfo(cfg.tz)) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    probed = analyzed = 0
    for mirror, entries in pending.items():
        keys = [e.rec.key for e in entries]
        recent_live = entries[0].rec.origin == "live" and entries[0].rec.date >= cutoff
        if recent_live and relayer.prober:
            relayer.prober.schedule(keys, mirror, due=0)
            probed += 1
        elif relayer.enricher and cfg.enrich_backlog:
            relayer.enricher.submit(keys, mirror)
            analyzed += 1
    log.info("待處理：%d 則鏡像先做刪除同步探測、%d 則排入 AI 分析", probed, analyzed)


async def poll_new_records(relayer: Relayer, every: float = 300) -> None:
    """每 5 分鐘讀 #關鍵字庫 的新紀錄（例如匯入工具寫的 enriched: no），排入分析。"""
    while True:
        await asyncio.sleep(every)
        try:
            new = await relayer.index.poll_new() if relayer.index else []
            groups: dict[str, list] = {}
            for e in new:
                if not e.rec.enriched:
                    groups.setdefault(e.rec.mirror, []).append(e.rec.key)
            if relayer.enricher:
                for mirror, keys in groups.items():
                    relayer.enricher.submit(keys, mirror)
            if relayer.ocr is not None and new:
                relayer.ocr.submit_entries(new)
            if groups:
                log.info("發現 %d 則新的未分析鏡像，已排入佇列", len(groups))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("讀取新紀錄失敗")


async def run_recap(args: argparse.Namespace) -> int:
    """手動產生（或重新產生）某一天的每日總結，方便測試；可以和 ./run.sh 同時跑（不收 Telegram）。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from kwindex import KwIndex
    from memory import MemoryStore, PromptStore
    from recap.runner import RecapChannel, Recapper

    cfg = config.load()
    if not (cfg.ai_server and cfg.ai_model):
        log.error("沒有設定 LOCAL_AI_SERVER／LOCAL_AI_MODEL")
        return 2
    llm = LLM(cfg.ai_server, cfg.ai_model, cfg.ai_key, cfg.ai_image_support)
    dc = DiscordOut(cfg.dc_token, cfg.dc_guild_id)
    try:
        await dc.start()
        bot_id = dc.client.user.id  # type: ignore[union-attr]
        guild = dc.guild
        editors = set(cfg.memory_editors) | ({guild.owner_id} if guild and guild.owner_id else set())
        db = await dc.channel(cfg.ch_db)
        await PromptStore(db, None, bot_id, editors, cfg.tz).load(publish_defaults=False)
        await MemoryStore(llm, db, None, bot_id, editors, cfg.tz).load()
        index = KwIndex(await dc.channel(cfg.ch_keywords), bot_id)  # type: ignore[arg-type]
        await index.load()
        rc = RecapChannel(await dc.channel(cfg.ch_recap), bot_id)  # type: ignore[arg-type]
        await rc.load()
        st = state.DbRecord(db, bot_id, "STATE")
        await st.load()
        dirty = state.DirtyDays(state.DbRecord(db, bot_id, "RECAP_DIRTY"))
        r = Recapper(cfg, llm, index, rc, st, dirty)
        day = args.date or (datetime.now(ZoneInfo(cfg.tz)) - timedelta(days=1)).strftime("%Y-%m-%d")
        if args.year:
            await r.summarize_year(args.year)
            log.info("✅ 已產生 %s 年總結", args.year)
        elif args.month:
            await r.summarize_month(args.month)
            log.info("✅ 已產生 %s 月總結", args.month)
        else:
            if not r._entries(day):
                log.error("%s 在 #關鍵字庫 沒有任何紀錄", day)
                return 1
            await r.summarize_day(day)
            log.info("✅ 已產生 %s 每日總結（到 #總結 查看）", day)
        return 0
    finally:
        await dc.close()


async def run_reset_cmd(args: argparse.Namespace) -> int:
    from reset import run_reset
    cfg = config.load()
    dc = DiscordOut(cfg.dc_token, cfg.dc_guild_id, read_messages=True)   # 要看得到你手動發的 #記憶
    try:
        await dc.start()
        return await run_reset(cfg, dc, args.yes, args.memory, args.chats, args.prompts, args.all)
    finally:
        await dc.close()


async def run_import_extra(args: argparse.Namespace) -> int:
    from importer.extra_job import run_extra
    cfg = config.load()
    if args.dry_run:                      # 試跑只解析檔案，不連 Discord
        return await run_extra(cfg, None, args.path, args.date_from, args.date_to, True)
    dc = DiscordOut(cfg.dc_token, cfg.dc_guild_id)
    try:
        await dc.start()
        return await run_extra(cfg, dc, args.path, args.date_from, args.date_to, False)
    finally:
        await dc.close()


async def run_import(args: argparse.Namespace) -> int:
    from importer.job import ImportJob

    cfg = config.load()
    dc = DiscordOut(cfg.dc_token, cfg.dc_guild_id)
    try:
        await dc.start()
        problems = await dc.check_channels(cfg.channel_map())
        for p in problems:
            log.error("頻道檢查：%s", p)
        if problems:
            return 2
        job = ImportJob(cfg, dc, args.path, args.date_from, args.date_to,
                        args.append_out_of_order, args.dry_run)
        return await job.run()
    finally:
        await dc.close()


def _watch_orphan() -> None:
    """由 App 啟動時：App 結束（父行程變成 launchd）就結束自己。啟動階段還沒開始讀 stdin、也還沒等結束訊號時，
    只靠 IPC 的 stop／EOF 收不到通知，會變成殘留行程佔著單一實例鎖。"""
    import os
    import threading
    import time
    parent = os.getppid()

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(2)
        log.warning("App 已結束，核心跟著結束")
        try:
            os.kill(os.getpid(), signal.SIGTERM)          # 優雅關閉（已走到等待結束訊號時會發「已離線」通知）
        except OSError:
            pass
        time.sleep(30)
        os._exit(0)                                       # 優雅關閉沒完成（例如還在啟動階段）就強制結束
    threading.Thread(target=watch, daemon=True, name="orphan-watch").start()


def main() -> None:
    parser = argparse.ArgumentParser(description="InboxRelay core")
    parser.add_argument("--check", action="store_true", help="只檢查設定與權限")
    sub = parser.add_subparsers(dest="cmd")
    imp = sub.add_parser("import", help="從 Telegram Desktop 匯出檔匯入歷史訊息")
    imp.add_argument("--path", required=True, help="匯出資料夾（含 result.json）或 result.json 路徑")
    imp.add_argument("--from", dest="date_from", help="起始日期 YYYY-MM-DD（含）")
    imp.add_argument("--to", dest="date_to", help="結束日期 YYYY-MM-DD（含）")
    imp.add_argument("--dry-run", action="store_true", help="只掃描與檢查，不發任何訊息")
    imp.add_argument("--append-out-of-order", action="store_true",
                     help="允許補匯入比頻道現有資料更舊的訊息（會加分隔標記）")
    ex = sub.add_parser("import-extra", help="匯入補充資料（電子發票 CSV、通用 JSONL）到 #補充資料")
    ex.add_argument("--path", required=True, help="檔案或資料夾（會掃描底下所有檔案）")
    ex.add_argument("--from", dest="date_from", help="起始日期 YYYY-MM-DD（含）")
    ex.add_argument("--to", dest="date_to", help="結束日期 YYYY-MM-DD（含）")
    ex.add_argument("--dry-run", action="store_true", help="只解析並顯示統計與範例，不連 Discord、不發訊息")
    rs = sub.add_parser("reset", help="清除測試資料（鏡像、關鍵字庫、總結、截圖、狀態紀錄），準備重新匯入")
    rs.add_argument("--yes", action="store_true", help="不再詢問確認")
    rs.add_argument("--memory", action="store_true", help="連 #記憶 一起清掉（含你手動發的）")
    rs.add_argument("--chats", action="store_true", help="連 #搜尋、搜尋測試、AI 測試、通知與互動 的對話一起清掉")
    rs.add_argument("--prompts", action="store_true", help="連 #提示詞 一起清掉（下次啟動重新發預設版）")
    rs.add_argument("--all", action="store_true", help="全部清空：所有相關頻道裡任何人發的所有訊息")
    rc = sub.add_parser("recap", help="手動產生某一天的每日總結（或 --month 月總結）")
    rc.add_argument("--date", help="日期 YYYY-MM-DD（預設昨天；可以是今天，方便測試）")
    rc.add_argument("--month", help="改產生月總結 YYYY-MM")
    rc.add_argument("--year", help="改產生年總結 YYYY（用該年各月的月總結）")
    args = parser.parse_args()
    setup_logging()
    import ipc
    if ipc.enabled():
        _watch_orphan()
    if args.cmd == "recap":
        sys.exit(asyncio.run(run_recap(args)))
    # import-extra 不收 Telegram，可以和核心（App）同時跑：核心每 5 分鐘會讀到新紀錄、排進夜間 AI
    if not args.check and args.cmd != "import-extra" and not single_instance():
        log.error("InboxRelay 已經在執行（選單列 App 或另一個 Terminal 分頁）。請先結束它（App 選單 →「結束 InboxRelay」），再重新執行。")
        sys.exit(4)
    if args.cmd == "reset":
        sys.exit(asyncio.run(run_reset_cmd(args)))
    if args.cmd == "import-extra":
        logging.getLogger().setLevel(logging.WARNING)
        sys.exit(asyncio.run(run_import_extra(args)))
    if args.cmd == "import":
        # 匯入時只顯示警告以上，避免洗掉進度列
        logging.getLogger().setLevel(logging.WARNING)
        sys.exit(asyncio.run(run_import(args)))
    sys.exit(asyncio.run(run(args.check)))


if __name__ == "__main__":
    main()
