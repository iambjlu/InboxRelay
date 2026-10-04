# relay-core/config.py
"""讀取 .env 設定。Token 絕不寫進日誌。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

REQUIRED = [
    "TG_BOT_TOKEN",
    "TG_INBOX_CHAT_ID",
    "DC_BOT_TOKEN",
    "DC_GUILD_ID",
    "DC_CH_INBOX_MIRROR",
    "DC_CH_RECAP",
    "DC_CH_SEARCH",
    "DC_CH_KEYWORDS",
    "DC_CH_DB",
]


def load_env_file(path: Path) -> None:
    """簡易 .env 解析：KEY=VALUE，忽略註解與空行；不覆蓋已存在的環境變數。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    tg_token: str
    tg_inbox_chat_id: int
    tg_probe_chat_id: int | None
    dc_token: str
    dc_guild_id: int
    ch_inbox_mirror: int
    ch_history_mirror: int | None  # v0.3 起停用，歷史與實時共用 #投遞箱鏡像
    ch_recap: int
    ch_search: int
    ch_keywords: int
    ch_db: int
    tz: str
    header_style: str  # "large"（# 日期）或 "small"（-# 日期）
    src_link: bool
    upload_limit_mb: float  # Discord 單檔上傳上限；2026 年起免費伺服器為 20MB
    ai_server: str | None = None
    ai_model: str | None = None
    ai_key: str = "none"
    ai_image_support: bool = True
    ch_ai_test: int | None = None  # AI 測試頻道（需要 Message Content Intent）
    ch_notify: int | None = None   # 通知頻道（記憶升級等）
    memory_editors: frozenset = frozenset()  # 可手動發 #記憶 的使用者 ID（伺服器擁有者自動允許）
    ch_search_test: int | None = None
    # 功能開關（第 8 節；之後會改存 #資料庫 TOGGLES）
    link_summary: bool = True
    link_screenshot: bool = False   # 連結截圖預覽（需要 tools/linkshot，run.sh 會自動編譯）
    ch_screenshot: int | None = None  # 截圖專用頻道
    ch_extra: int | None = None       # 補充資料（發票、日記…）
    ch_chat: int | None = None        # AI 對話（像 ChatGPT，每個對話一個對話串）
    ch_chat_prompt: int | None = None # AI 對話的提示詞與記憶
    llm_context: int = 131072         # 外部 LLM 的 context window（token）
    chat_compact_pct: int = 90        # AI 對話用到 context 的幾 % 就自動壓縮
    private_urls: bool = True       # 內網／本機網址也截圖、也做連結摘要（實驗用）
    ignore_cert: bool = False       # 憑證無效的網站也強制開啟（截圖、連結摘要；只讀取，不送任何資料）
    reverse_sync: bool = True       # 在 #投遞箱鏡像 直接發訊息 → 也發到 Telegram（編輯、刪除同步）
    memory_soft_mb: float = 600     # 超過就清快取
    memory_hard_mb: float = 1000    # 連續超過就重新啟動（App 管理時）
    image_summary: bool = True
    document_summary: bool = True
    keywords: bool = True
    delete_sync: bool = True
    edit_sync: bool = True
    edit_sync_hours: float = 24
    probe_delay_sec: float = 300
    enrich_backlog: bool = True
    recap: bool = True
    search_live: bool = True    # 正式 #搜尋 頻道（搜 #關鍵字庫＋#總結）
    search_thinking: bool = False  # 搜尋回答時讓 Qwen 先思考（較準、較慢）
    recap_wait_hours: float = 12
    # 排程（第 12.3 節）：always 永遠立即執行／daily 每天自動開始到做完為止／window 每天自動開始與暫停／manual 手動執行
    llm_schedule: str = "daily"   # 外部 LLM：開始時間＝recap_time
    llm_end: str = "08:00"         # 只有 window 模式會用到
    ane_schedule: str = "always"  # 本機 ANE（圖片文字辨識，在選單列 App 裡跑）
    ane_start: str = "03:30"
    ane_end: str = "08:00"
    shot_schedule: str = "always"  # 連結截圖（獨立車道，預設永遠立即執行）
    shot_start: str = "03:30"
    shot_end: str = "08:00"
    recap_time: str = "03:30"    # 每天幾點開始跑總結（含補跑積欠的）；按 n 可立即總結、p 暫停

    def channel_map(self) -> dict[str, int]:
        return {
            "投遞箱鏡像": self.ch_inbox_mirror,
            "總結": self.ch_recap,
            "搜尋": self.ch_search,
            "關鍵字庫": self.ch_keywords,
            "資料庫": self.ch_db,
        }

    def channel_map_with_test(self) -> dict[str, int]:
        m = self.channel_map()
        if self.ch_ai_test:
            m["AI 測試"] = self.ch_ai_test
        if self.ch_notify:
            m["通知"] = self.ch_notify
        if self.ch_search_test:
            m["搜尋測試"] = self.ch_search_test
        if self.ch_screenshot:
            m["連結預覽與進度"] = self.ch_screenshot
        return m


def load() -> Config:
    load_env_file(Path(os.environ.get("INBOXRELAY_ENV", DEFAULT_ENV_PATH)))
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"[config] .env 缺少設定：{', '.join(missing)}")

    style = os.environ.get("HEADER_STYLE", "large").strip().lower()
    if style not in ("large", "small"):
        raise SystemExit("[config] HEADER_STYLE 只能是 large 或 small")

    probe = os.environ.get("TG_PROBE_CHAT_ID")
    e = os.environ
    return Config(
        tg_token=e["TG_BOT_TOKEN"],
        tg_inbox_chat_id=int(e["TG_INBOX_CHAT_ID"]),
        tg_probe_chat_id=int(probe) if probe else None,
        dc_token=e["DC_BOT_TOKEN"],
        dc_guild_id=int(e["DC_GUILD_ID"]),
        ch_inbox_mirror=int(e["DC_CH_INBOX_MIRROR"]),
        ch_history_mirror=int(e["DC_CH_HISTORY_MIRROR"]) if e.get("DC_CH_HISTORY_MIRROR") else None,
        ch_recap=int(e["DC_CH_RECAP"]),
        ch_search=int(e["DC_CH_SEARCH"]),
        ch_keywords=int(e["DC_CH_KEYWORDS"]),
        ch_db=int(e["DC_CH_DB"]),
        tz=e.get("TZ", "Asia/Taipei") or "Asia/Taipei",
        header_style=style,
        src_link=_bool("SRC_LINK", True),
        upload_limit_mb=float(e.get("DC_UPLOAD_LIMIT_MB") or 20),
        ai_server=e.get("LOCAL_AI_SERVER") or None,
        ai_model=e.get("LOCAL_AI_MODEL") or None,
        ai_key=e.get("LOCAL_AI_API_KEY") or "none",
        ai_image_support=_bool("LOCAL_AI_IMAGE_SUPPORT", True),
        ch_ai_test=int(e["DC_CH_AI_TEST"]) if e.get("DC_CH_AI_TEST") else None,
        ch_notify=int(e["DC_CH_NOTIFY"]) if e.get("DC_CH_NOTIFY") else None,
        memory_editors=frozenset(int(x) for x in e.get("MEMORY_EDITORS", "").replace(" ", "").split(",") if x),
        ch_search_test=int(e["DC_CH_SEARCH_TEST"]) if e.get("DC_CH_SEARCH_TEST") else None,
        link_summary=_bool("LINK_SUMMARY", True),
        link_screenshot=_bool("LINK_SCREENSHOT", False),
        ch_screenshot=int(e["DC_CH_SCREENSHOT"]) if e.get("DC_CH_SCREENSHOT") else None,
        ch_extra=int(e["DC_CH_EXTRA"]) if e.get("DC_CH_EXTRA") else None,
        ch_chat=int(e["DC_CH_CHAT"]) if e.get("DC_CH_CHAT") else None,
        ch_chat_prompt=int(e["DC_CH_CHAT_PROMPT"]) if e.get("DC_CH_CHAT_PROMPT") else None,
        llm_context=int(e.get("LLM_CONTEXT") or 131072),
        chat_compact_pct=min(max(int(e.get("CHAT_COMPACT_PCT") or 90), 30), 98),
        private_urls=_bool("PRIVATE_URLS", True),
        ignore_cert=_bool("IGNORE_CERT_ERRORS", False),
        reverse_sync=_bool("REVERSE_SYNC", True),
        memory_soft_mb=float(e.get("MEMORY_SOFT_MB") or 600),
        memory_hard_mb=float(e.get("MEMORY_HARD_MB") or 1000),
        image_summary=_bool("IMAGE_SUMMARY", True),
        document_summary=_bool("DOCUMENT_SUMMARY", True),
        keywords=_bool("KEYWORDS", True),
        delete_sync=_bool("DELETE_SYNC", True),
        edit_sync=_bool("EDIT_SYNC", True),
        edit_sync_hours=float(e.get("EDIT_SYNC_HOURS") or 24),
        probe_delay_sec=float(e.get("PROBE_DELAY_SEC") or 300),
        enrich_backlog=_bool("ENRICH_BACKLOG", True),
        recap=_bool("RECAP", True),
        search_live=_bool("SEARCH_LIVE", True),
        search_thinking=_bool("SEARCH_THINKING", False),
        recap_wait_hours=float(e.get("RECAP_WAIT_HOURS") or 12),
        recap_time=e.get("RECAP_TIME") or "03:30",
        llm_schedule=(e.get("LLM_SCHEDULE") or ("daily" if _bool("AI_NIGHT_ONLY", True) else "always")).strip(),
        llm_end=(e.get("LLM_END") or "08:00").strip(),
        ane_schedule=(e.get("ANE_SCHEDULE") or "always").strip(),
        ane_start=(e.get("ANE_START") or "03:30").strip(),
        ane_end=(e.get("ANE_END") or "08:00").strip(),
        shot_schedule=(e.get("SHOT_SCHEDULE") or "always").strip(),
        shot_start=(e.get("SHOT_START") or "03:30").strip(),
        shot_end=(e.get("SHOT_END") or "08:00").strip(),
    )
