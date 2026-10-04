# relay-core/enrich/screenshot.py
"""連結截圖預覽（第 12 節，方案 A）：呼叫 tools/linkshot（macOS WebKit）取得 JPEG。

- 全程在記憶體：圖片從小工具的 stdout 讀進來，直接上傳 Discord。
- 每張截圖都是一個新的 linkshot 行程：非持久化資料儲存（Cookie、快取只在記憶體），
  拍完行程就結束，Cookie 與 WebKit 用掉的記憶體一起歸還系統。
- 只截 http/https。內網／本機網址（127.x、192.168.x、10.x、Tailscale 100.64/10…）預設也會截（PRIVATE_URLS=true），
  方便實驗；不想讓內網頁面出現在 Discord 就設 PRIVATE_URLS=false。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("screenshot")

TOOL = Path(__file__).resolve().parent.parent.parent / "tools" / "linkshot"
MAX_PNG = 8 * 1024 * 1024     # 圖片大小上限（JPEG 通常 0.3～2MB）
ALLOW_PRIVATE = True          # main 會依 .env 的 PRIVATE_URLS 設定
IGNORE_CERT = False           # main 會依 IGNORE_CERT_ERRORS 設定
WIDTH = 1280
MAX_HEIGHT = 4000             # 整頁截圖的最大高度（太長的頁面只拍到這裡）
CERT_WARN = "⚠️ 憑證無效，已強制開啟"


def available() -> bool:
    return TOOL.is_file() and TOOL.stat().st_mode & 0o111 != 0


def _is_private_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    cgnat = ipaddress.ip_network("100.64.0.0/10")          # Tailscale 等
    return (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast
            or a.is_unspecified or (a.version == 4 and a in cgnat))


async def is_public_url(url: str) -> bool:
    """網址是 http/https，而且主機解析出來全部是公開 IP。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal", ".lan", ".home.arpa")):
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                                             type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    ips = {i[4][0] for i in infos}
    return bool(ips) and not any(_is_private_ip(ip) for ip in ips)


SETTLE = 3          # 載入完成後等幾秒才拍


async def capture(url: str, timeout: float = 20) -> bytes | None:
    png, _ = await capture_with_reason(url, timeout)
    return png


async def capture_with_reason(url: str, timeout: float = 20) -> tuple[bytes | None, str]:
    """截圖：成功回傳 (PNG, "")；失敗回傳 (None, 原因)。"""
    if not available():
        return None, "找不到 tools/linkshot（重開 App 或 ./run.sh 會自動編譯）"
    if urlparse(url).scheme not in ("http", "https"):
        return None, "不是 http/https 網址"
    if not ALLOW_PRIVATE and not await is_public_url(url):
        log.info("略過截圖（內網網址，PRIVATE_URLS=false）：%s", url)
        return None, "內網網址（PRIVATE_URLS=false）"
    try:
        proc = await asyncio.create_subprocess_exec(
            str(TOOL), url, "--timeout", str(int(timeout)), "--settle", str(SETTLE),
            "--width", str(WIDTH), "--max-height", str(MAX_HEIGHT), *(["--insecure"] if IGNORE_CERT else []),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + SETTLE + 15)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        log.warning("截圖逾時：%s", url)
        return None, "逾時"
    except OSError as e:
        log.warning("無法執行 linkshot：%s", e)
        return None, f"無法執行 linkshot：{e}"
    msg = err.decode(errors="ignore").strip()
    if proc.returncode != 0 or not out.startswith((b"\xff\xd8", b"\x89PNG")) or len(out) > MAX_PNG:
        last = msg.splitlines()[-1].removeprefix("linkshot: ") if msg else ""
        reason = last or (f"結束碼 {proc.returncode}" if proc.returncode else "沒有輸出圖片")
        if len(out) > MAX_PNG:
            reason = "圖片太大"
        log.info("截圖失敗（%s）：%s｜%s", proc.returncode, reason, url)
        return None, reason[:150]
    return out, (CERT_WARN if "CERT_BYPASSED" in msg else "")


async def capture_many(urls: list[str], label: str = "連結截圖",
                       failures: list[tuple[str, str]] | None = None) -> list[tuple[str, bytes]]:
    """依序截圖並回報進度；回傳 [(網址, PNG)]；失敗的 (網址, 原因) 放進 failures。"""
    import activity
    out: list[tuple[str, bytes]] = []
    if not urls:
        return out
    if not available():
        if failures is not None:
            failures += [(u, "找不到 tools/linkshot") for u in urls]
        return out
    task = activity.start(label, total=len(urls))
    try:
        for u in urls:
            task.set(detail=u[:60])
            png, why = await capture_with_reason(u)
            if png:
                out.append((u, png))
                if why and failures is not None:
                    failures.append((u, why))      # 成功但有警告（憑證無效）
            elif failures is not None:
                failures.append((u, why))
            task.advance()
    finally:
        activity.finish(task, f"{len(out)}/{len(urls)} 張")
    return out


FAIL_MARK = "!"          # 紀錄 shot 欄位裡「無法截圖通知」貼文連結的前綴：最終失敗，不再重試（成功的截圖沒有前綴）
RETRY_MARK = "?"         # 「?<第幾次失敗><連結>」：還會再試的失敗通知（內網、不是 http、圖片太大以外的原因）


async def _host_resolves(url: str) -> bool:
    host = urlparse(url).hostname or ""
    try:
        await asyncio.get_running_loop().getaddrinfo(host, None)
        return True
    except (socket.gaierror, UnicodeError):
        return False


class NetworkDown(Exception):
    """截圖失敗，而且連 google.com 也連不上：是網路中斷，不是網站的問題（不發失敗通知，等網路恢復再做）。"""


PERMANENT_WHY = ("內網", "不是 http", "圖片太大")      # 這幾種原因重試也沒用


def is_permanent(why: str) -> bool:
    return any(k in (why or "") for k in PERMANENT_WHY)


async def network_ok(host: str = "www.google.com", timeout: float = 6) -> bool:
    """網路通不通：連得上 google.com:443 就算通。"""
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, 443), timeout)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def post_one(dc, channel_id: int, url: str, when: str, back_url: str,
                   back_label: str = "鏡像", tries: int = 1) -> tuple[str, bool, str]:
    """截一個網址並發到連結預覽頻道；拍不到就改發一則「📸 無法截圖」通知（含原因）。
    回傳 (貼文連結, 是否成功, 失敗原因)。失敗不在這裡反覆重試（交給車道之後排程重試）；
    失敗時若 google.com 也連不上 → 是網路中斷，丟 NetworkDown（不發通知）。圖只在記憶體裡。"""
    png, why = None, ""
    if not await _host_resolves(url):
        why = "網域不存在（DNS 查不到）"
    else:
        for i in range(tries):
            png, why = await capture_with_reason(url)
            if png:
                break
            if i + 1 < tries:
                await asyncio.sleep(3)
    if not png and not is_permanent(why) and not await network_ok():
        raise NetworkDown(why or "網路中斷")
    back = f"[↗ 跳到{back_label}](<{back_url}>)"
    if png:
        note = f"\n{CERT_WARN}" if why == CERT_WARN else ""
        msg = await dc.send_file(channel_id, f"📸 {when}｜{url}{note}\n{back}", "preview.jpg", png)
        png = None
        return msg.jump_url, True, ""
    msg = await dc.send(channel_id, f"📸 無法截圖｜{when}｜<{url}>\n-# 原因：{why or '未知'}\n{back}")
    return msg.jump_url, False, why or "未知"
