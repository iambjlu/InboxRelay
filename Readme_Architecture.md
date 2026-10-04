# InboxRelay — Architecture Reference

Machine-oriented reference. Each source file has its own section; when changing a file, update the section of that file (see CLAUDE.md). No secrets belong in this document.

## 0. Overview

```
Telegram group ──(python-telegram-bot polling)──► relay-core (Python 3.9+, asyncio, single process)
                                                    │  discord.py 2.x (bot)          ┌─► Discord guild channels (all persistent data)
                                                    │  OpenAI SDK ──────────────────►│   external LLM (OpenAI-compatible, e.g. oMLX)
                                                    │  stdin/stdout JSON lines ◄────►│   InboxRelay.app (SwiftUI MenuBarExtra)
                                                    │                                 │     └─ Vision OCR (Apple Neural Engine), Keychain, settings
                                                    └─ subprocess tools/linkshot (WebKit screenshot → JPEG on stdout)
```

- Persistence: Discord only (records, state, memory, prompts, summaries). Local disk: venv, lock, one-time flags (see §5); no log files are written (the App keeps an in-memory 500-line log buffer).
- Media (attachments, images, pages, screenshots) handled as in-memory bytes; never written to disk.
- One core instance per machine (`data_dir/instance.lock`) and per guild (host heartbeat in `#資料庫 STATE`).
- Two work lanes (`lanes.py`): `llm` (external LLM background work) and `ane` (local OCR via the App). Interactive features (search, chat, feedback, AI test) bypass lanes.

## 1. Repository layout

```
InboxRelay/
├── Readme.md                 user-facing readme
├── Readme_Architecture.md    this file
├── 使用說明.md                CLI usage (run.sh subcommands, .env fields, exit codes)
├── InboxRelay-實作指南.md     original design spec (section numbers referenced in docstrings, e.g. 第 7.3 節)
├── CLAUDE.md                 rules for AI agents working on this repo
├── run.sh                    dev launcher: .venv + deps + compile tools/linkshot → python relay-core/main.py "$@"
├── build_app.sh              swiftc app/*.swift → build/InboxRelay.app; bundles relay-core (no tests) + core_run.sh + linkshot + icon
├── install_app.sh            build_app.sh → quit running app → replace /Applications/InboxRelay.app → open (`--instant`: open with `--args --instant`)
├── app/                      SwiftUI menu-bar app (§4)
├── tools/                    linkshot.swift (+ Info.plist ATS exception), make_icon.swift
└── relay-core/               Python core (§3); tests/ (unittest)
```

Git-ignored: `.env`, `.venv/`, `__pycache__/`, `app/.build.log`, `build/`, `tools/linkshot`.

## 2. Configuration (environment variables)

Source of values: App settings (Keychain for secrets, UserDefaults or synced-folder `settings.json` for the rest) passed as env to the core; empty fields fall back to `.env` (`INBOXRELAY_ENV`, default `<repo>/.env` for run.sh, `data_dir/.env` for the App). Parsed in `config.py` → frozen dataclass `Config`.

| Env | Config field | Default | Meaning |
|---|---|---|---|
| TG_BOT_TOKEN * | tg_token | — | Telegram bot token |
| TG_INBOX_CHAT_ID * | tg_inbox_chat_id | — | source group id |
| TG_PROBE_CHAT_ID | tg_probe_chat_id | None | probe chat for delete detection (probe.py) |
| DC_BOT_TOKEN * | dc_token | — | Discord bot token |
| DC_GUILD_ID * | dc_guild_id | — | guild |
| DC_CH_INBOX_MIRROR * | ch_inbox_mirror | — | #投遞箱鏡像 |
| DC_CH_RECAP * | ch_recap | — | #總結 |
| DC_CH_SEARCH * | ch_search | — | #搜尋 |
| DC_CH_KEYWORDS * | ch_keywords | — | #關鍵字庫 |
| DC_CH_DB * | ch_db | — | #資料庫 |
| DC_CH_NOTIFY | ch_notify | None | 通知與互動 (feedback.py, memory notices) |
| DC_CH_AI_TEST | ch_ai_test | None | AI 測試 (aitest.py) |
| DC_CH_SEARCH_TEST | ch_search_test | None | 搜尋測試 |
| DC_CH_SCREENSHOT | ch_screenshot | None | 連結預覽與進度 (screenshots + activity message) |
| DC_CH_EXTRA | ch_extra | None | 補充資料 (importer/extra) |
| DC_CH_CHAT | ch_chat | None | AI 對話 (chat.py) |
| DC_CH_CHAT_PROMPT | ch_chat_prompt | None | AI 對話 #提示詞／#記憶 |
| DC_CH_HISTORY_MIRROR | ch_history_mirror | None | deprecated |
| TZ | tz | Asia/Taipei | |
| HEADER_STYLE | header_style | large | `large` (# date) / `small` (-# date) |
| SRC_LINK | src_link | true | add Telegram source link |
| DC_UPLOAD_LIMIT_MB | upload_limit_mb | 20 | per-file upload cap |
| LOCAL_AI_SERVER / LOCAL_AI_MODEL / LOCAL_AI_API_KEY | ai_server / ai_model / ai_key | None/None/"none" | OpenAI-compatible endpoint |
| LOCAL_AI_IMAGE_SUPPORT | ai_image_support | true | model accepts images |
| LLM_CONTEXT | llm_context | 131072 | context window (tokens) for chat budget |
| CHAT_COMPACT_PCT | chat_compact_pct | 90 (clamped 30–98) | auto-compact threshold |
| MEMORY_EDITORS | memory_editors | ∅ | user ids allowed to post #記憶 / feedback (guild owner always allowed) |
| LINK_SUMMARY | link_summary | true | |
| LINK_SCREENSHOT | link_screenshot | false | requires tools/linkshot |
| PRIVATE_URLS | private_urls | true | allow LAN/localhost URLs |
| IGNORE_CERT_ERRORS | ignore_cert | false | |
| REVERSE_SYNC | reverse_sync | true | Discord mirror posts → Telegram |
| IMAGE_SUMMARY / DOCUMENT_SUMMARY / KEYWORDS | image_summary / document_summary / keywords | true | |
| DELETE_SYNC / EDIT_SYNC | delete_sync / edit_sync | true | |
| EDIT_SYNC_HOURS | edit_sync_hours | 24 | |
| PROBE_DELAY_SEC | probe_delay_sec | 300 | |
| ENRICH_BACKLOG | enrich_backlog | true | queue un-enriched records at startup |
| RECAP | recap | true | |
| RECAP_WAIT_HOURS | recap_wait_hours | 12 | |
| RECAP_TIME | recap_time | 03:30 | llm lane start time |
| LLM_SCHEDULE | llm_schedule | daily (legacy AI_NIGHT_ONLY=false → always) | always/daily/window/manual |
| LLM_END | llm_end | 08:00 | window mode only |
| ANE_SCHEDULE / ANE_START / ANE_END | ane_schedule / ane_start / ane_end | always / 03:30 / 08:00 | |
| SEARCH_LIVE / SEARCH_THINKING | search_live / search_thinking | true / false | |
| MEMORY_SOFT_MB / MEMORY_HARD_MB | memory_soft_mb / memory_hard_mb | 600 / 1000 | memguard |

`*` = required (`config.REQUIRED`). Process-level env (not in Config): `INBOXRELAY_IPC=1` (enable IPC), `INBOXRELAY_DATA` (data dir), `INBOXRELAY_ENV` (.env path), `STATUS_BAR=false` (terminal status bar off).

## 3. relay-core modules

### main.py
Entry point. Subcommands: (none) live core; `--check`; `import --path [--from --to --dry-run --append-out-of-order]`; `import-extra --path [--from --to --dry-run]`; `reset [--yes --memory --chats --prompts --all]`; `recap [--date|--month|--year]`. Exit codes: 0 ok, 2 config/permission, 3 import running, 4 already running on this machine, 5 running on another host.
`run(check_only)` wiring order: `lanes.setup` → Discord login + permission check → LLM ping (failure → `lanes.get("llm").go_offline`) → `MemoryStore` + `PromptStore.load` → `mem.refresh_once("memory-rules-v2", …)` → Telegram → `Relayer`/`Enricher`/`Prober` → recap (`Recapper`, `run_lane(llm, cycle=recapper.cycle)`) → IPC (`OcrWorker`, `enricher.on_link_images = ocr.submit`, `run_lane(ane, cycle=ocr.cycle)`) → background `_refresh_links` (waits `enricher.drained`, then `enrich.refresh.refresh_links`) → `poll_new_records` every 300 s → `queue_backlog` (un-enriched records: recent live → probe, else enrich).
Functions: single_instance, setup_logging, install_console (terminal keys n/p/r/s/h), notify, online_message, status_bar_lines, queue_backlog, poll_new_records, run_recap, run_reset_cmd, run_import_extra, run_import.

### config.py
`Config` dataclass, `load()`, `load_env_file()` (does not override existing env), `REQUIRED`. `channel_map()` / `channel_map_with_test()` for permission checks. See §2.

### paths.py
`data_dir()` → `INBOXRELAY_DATA` or `~/Library/Application Support/InboxRelay` (created on demand).

### lanes.py
`Lane(name, label, mode, start, end, tz)`; `MODES = always|daily|window|manual`; `ALWAYS_CHECK_SEC = 600`.
State fields: `open`, `manual`, `paused`, `available` (ane: App connected), `offline` (llm unreachable), `next_start`, `close_at`. `state` → `unavailable|paused|offline|running|waiting`. `allowed()` = available ∧ ¬paused ∧ ¬offline ∧ (always ∨ open).
Methods: `gate()` (await until allowed), `pause()`, `resume()`, `run_now()`, `go_offline(why)` (ends current round; work kept; retried next start or after 600 s in always mode), `describe()`.
`run_lane(lane, drained, cycle=None)`: loop {wait start/trigger → clear `offline` → run `cycle()` then wait `drained()` → stop on close_at / done / offline}. Globals `LANES`, `setup(cfg)` creates `llm` (start=RECAP_TIME, end=LLM_END) and `ane`, `get(name)`.

### activity.py
Progress tracking for background work. `Task` (label, done, total, detail, item steps, llm_frac), `Activity` (tasks, Discord progress message `STATUS_HEAD` in 連結預覽與進度 or #關鍵字庫 after `DISCORD_AFTER_SEC=10`, edited every `DISCORD_EVERY_SEC=15`, deleted when idle; `status_lines()` adds lane lines incl. "🔌 外部 LLM 連不上"). Module API: `init, start(label,total,background), finish, bind, bound, step, begin_item, gate(lane_name="llm")`.

### llm.py
`LLM(base_url, model, api_key, image_support)` over `AsyncOpenAI(timeout=300, max_retries=1)`; single-flight `Semaphore(1)`.
- `chat(prompt, data=None, image=None, image_mime, max_tokens, thinking, use_memory)` → str. System = `GUARD` + `MEMORY_WRAP(memory)`. Background calls (bound activity task) stream for progress and yield to interactive callers (`_interactive_waiting`). `<think>` stripped.
- `chat_messages(messages, max_tokens, thinking, temperature, progress=None)` → (text, usage) for multi-turn chat; `progress(n_tokens)` enables streaming.
- `ping()` → model ids. `memory`, `memory_version` set by MemoryStore.
- `is_offline_error(e)`: openai.APIConnectionError / InternalServerError / NotFoundError, httpx.ConnectError / TimeoutException, ConnectionError → treat as "LLM offline" (keep work).

### prompts.py
Default prompt texts; effective text = latest `#提示詞 NAME` in #資料庫 (`get`, `render(name, **kw)` formats only names in `PLACEHOLDERS`, `validate`, `set_current`, `reset`). Names (`DEFAULTS`): GUARD, MEMORY_WRAP, LINK, LINK_FALLBACK, IMAGE, DOC, DOC_PART, DOC_MERGE, KEYWORDS, RECAP_DAILY, RECAP_MONTHLY, RECAP_YEARLY, SEARCH_PARSE, SEARCH_ANSWER, SEARCH_FOLLOWUP, GENERAL_ANSWER, FEEDBACK_ROUTE, MEMORY_UPGRADE, CHAT_MEMORY_UPGRADE, CHAT_COMPACT.
Placeholders: MEMORY_WRAP{memory}, DOC{name}, DOC_PART{name,i}, RECAP_DAILY{date}, RECAP_MONTHLY{month}, RECAP_YEARLY{year}, SEARCH_PARSE/FEEDBACK_ROUTE/SEARCH_FOLLOWUP{today,weekday}.
MEMORY_UPGRADE sections: 【詞彙】【常見主題】【整理偏好】【待確認】 (待確認 ≤3, only recurring unclear abbreviations; excludes follow-ups, relationships, one-off events, numeric logs). CHAT_MEMORY_UPGRADE sections: 【關於使用者】【偏好的回答方式】【進行中的事】【待確認】. Output format `=== 記憶 === … === 升級重點 === - …`.

### memory.py
- `MemoryStore(llm, db_channel, notify_channel, bot_id, editors, tz, apply_to_llm=True, upgrade_prompt="MEMORY_UPGRADE", compress_hint, where, label, obs_label)`: latest `#記憶 vN｜YYYY-MM-DD HH:MM｜source` message (body in code block) from bot/owner/editors. `observe(text, kw, gist)` → True after `UPGRADE_EVERY=30`; `add_correction(text)`; `upgrade(reason, urgent=False)` (LLM → `parse_upgrade` → compress if > `TARGET_MEMORY_CHARS=1200`, hard cap `MAX_MEMORY_CHARS=1800` → `publish`); `refresh_once(flag, note)` one-time re-organize under new rules (offline → wait lane, retry); `publish` posts new version and notifies.
- `PromptStore(db_channel, notify_channel, bot_id, editors, tz).load(publish_defaults)`: reads `#提示詞 NAME vN｜…` messages, user edits marked `CUSTOM_MARK="（自訂）"`, republishes a full set when defaults change; groups `PROMPT_GROUPS`.
- Helpers: `wrap_block/unwrap_block`, `fit_memory`, `parse_prompt_message`, `build_prompt_messages`. Constants `MEM_TAG="#記憶"`, `PROMPT_TAG="#提示詞"`, `MSG_LIMIT=1950`.

### records.py
#關鍵字庫 record format (one Discord message per Telegram message, ≤ `MAX_LEN=2000`):
```
KW|v2
tg_chat: <int>            tg_msg: <int>
origin: live|import|dc|extra
date: YYYY-MM-DD HH:MM    type: text|image|album|file|link|voice|video|sticker|invoice
mirror: <discord message url>
[src|filename|fid|reply|shot|img|kw|gist]: …   (only when set; one line each)
[ocr: 見 OCR 訊息 | （無文字） | （無法取得圖片）]
[excerpt: …]
enriched: yes|no
```
`KwRecord` dataclass fields: tg_chat, tg_msg, origin, date, type, mirror, src, filename, fid, reply (AI reply url), shot (screenshot post urls, space-separated), kw, gist, excerpt, enriched, ocr, img (image URLs found in the text, space-separated). Key = (tg_chat, tg_msg); extra imports use tg_chat=0.
OCR text lives in a separate message `OCR|tg_chat|tg_msg\n<text>` (≤ `OCR_ROOM=1960`). Constants: `PREFIX="KW|v2"`, `OCR_PREFIX`, `OCR_REF="見 OCR 訊息"`, `OCR_MARKERS=("（無文字）","（無法取得圖片）")`, legacy `OCR_NL=" ⏎ "`.
Functions: format_kw, parse_kw, parse_key, from_dict, parse_record, ocr_message, parse_ocr_message, flat_ocr, make_excerpt(200), one_line, parse_message_url.

### kwindex.py
`KwIndex`: in-memory map key → `KwEntry(msg_id, rec, ocr_msg, legacy)`; loads whole #關鍵字庫 at startup, `poll_new()` incremental; `update(key, **changes)` edits the record message and syncs its OCR message (`_sync_ocr`); `by_mirror(url)`, `get`, `delete` (also deletes OCR message).

### state.py
#資料庫 fixed records (first line tag, then `key: value`, edited in place): `STATE` (host heartbeat; `HOST_STALE=3 min`), `IMPORT` (import lock/checkpoint; `LOCK_STALE`), `LIVE`, `RECAP_DIRTY` (`DirtyDays`: days to re-summarize), `RECAP_NOTES` (per-day user notes). `DbRecord`, `this_host`, `other_host_active`, `import_lock_active`.

### formatting.py
Pure helpers for mirror message text: date headers (`header`, `parse_header_date`, `WEEKDAYS`), `build_mirror_content`, `build_live_message`, `oversize_line(name)` → `📎 name（過大未搬移）`, `truncate`, `supergroup_src_url`, `DISCORD_LIMIT`.

### tg_intake.py / album.py
`TelegramIntake`: polling, group migrate handling, `classify(msg)` → (type, `AttachmentRef(file_id, file_name, file_size, file_unique_id)`, text); photo name `photo_<msg_id>.jpg`; album pieces (media_group_id, buffered 2 s by `AlbumBuffer`) get `type="album"`. Emits `RelayItem(pieces: list[TgPiece])` to a queue. Edits → `_handle_edit`.

### relay.py
`Relayer`: consumes RelayItems; dedupe by index; attachments grouped by `plan_groups` (≤10 files and ≤ size limit per message), downloaded per group into memory; oversize/failed → `📎` lines; writes KW records; hands off to `Prober` (live) then `Enricher`; edit sync (re-upload when fid changes), delete sync; `TextCache` (full text for live messages, `TEXT_CACHE_MAX`). Attributes `enricher`, `ocr`, `prober`, `dirty`, `index`, `text_cache`.

### probe.py
`Prober`: after `PROBE_DELAY_SEC`, `copyMessage` original to probe chat; "message to copy not found" → delete mirror + record; else allow enrichment.

### dc_output.py
`DiscordOut`: client wrapper; `channel(id)`, `send(channel_id, content, files=[(name, bytes)], retries=3)`, `send_file`, `fetch_by_url`, `delete_by_url`, `load_kw_keys`, `filesize_limit`, permission check (`view_channel, send_messages, read_message_history, attach_files, embed_links, manage_messages`), `add_message_handler`.

### dc_reverse.py
`ReverseSync`: messages posted by users in #投遞箱鏡像 → sent to Telegram (photo ≤10 MB, files ≤50 MB, caption ≤1024); record origin=`dc`, mirror = the user's own message; edits/deletes mirrored.

### enrich/pipeline.py
`Enricher(cfg, dc, llm, index, text_cache, memory)`; one job = `EnrichJob(keys, mirror_url)`.
Queue: `asyncio.Queue` + `_front` deque (jobs returned after LLM offline, processed first); `backlog`, `drained`; `submit()` dedupes by mirror url. `on_link_images: callable(mirror_url)` (set to `OcrWorker.submit`).
`run()`: `activity.gate()` + `lanes.get("llm").gate()` → `process(job)`; on `is_offline_error` → `_undo_posted()` (delete reply/screenshot posts of this job), push job to `_front`, `lane.go_offline()`; other errors logged and job dropped (record stays enriched=no; re-queued at next startup).
`process(job)`: collect URLs → `probe_link` (≤3) → screenshots for web URLs (`post_screenshots`, ≤2, PNGs kept in memory) → `summarize_probed` per URL (image URL → 🖼️ part; unreadable page → preview+screenshot fallback) → attachments (`summarize_image`, `summarize_document`, ≤ `MAX_FILE=20MB`) → one reply under the mirror (`daily.split_message`) → per key `extract_keywords` + `memory.observe` → `index.update(kw, gist, enriched=True, reply, shot, img)`.

### enrich/link.py
`find_urls` (`URL_RE`, ASCII only). `fetch_link(url, want_image)` → (text, kind, bytes) with kind `article|og|file|image|""` (HTML ≤ `MAX_BYTES=3MB`, image ≤ `IMAGE_MAX=20MB`, non-HTML only headers read → `file_link_note`). `fetch_text(url)` → (text, kind) (used by chat.py). `probe_link(url)` → (text, kind, bytes, err) with kind `private` when blocked by PRIVATE_URLS. `embed_preview(message, url)` → Discord embed (site/author/title/description). `summarize_probed(llm, url, probed, preview, shot, caption, describe_image)` → `LinkResult(title, summary, image, failed)`; fallback uses prompt `LINK_FALLBACK` and appends `（根據網頁標籤、網頁預覽、截圖）`. `summarize_link(llm, url)` → str (aitest). `sniff_mime`, `human_size`, `UA`.

### enrich/screenshot.py
Runs `tools/linkshot <url> --timeout --settle 5 --width 1280 --max-height 4000 [--insecure]`; image from stdout (JPEG/PNG ≤ `MAX_PNG=8MB`); non-persistent WebKit store. `capture`, `capture_with_reason`, `capture_many(urls, label, failures)`, `post_screenshots(dc, channel_id, urls, when, back_url, back_label, pngs=None)` → post urls (and fills `pngs[url]`). Failures are only logged to the core logger (no file). `ALLOW_PRIVATE`, `IGNORE_CERT`, `CERT_WARN`, `is_public_url`.

### enrich/refresh.py
One-time backfill (flag `link-refresh-v1`): for records with `reply` and a URL in the excerpt, fetch the reply message, find blocks `**🔗|🖼️ url**` + `> …` lines whose body `needs_refresh` (無法讀取 / 僅根據網頁標題與描述 / 檔案下載連結+image/), redo with `summarize_probed` (old screenshot from `rec.shot` or a fresh capture, not posted), edit the reply (`join_fit` ≤2000), set `img` and notify OCR. Offline → `go_offline`, wait lane, retry same item. Functions: needs_refresh, parse_segments, join_fit, refresh_one, refresh_links.

### enrich/image.py / document.py / keywords.py
- image: `summarize_image(llm, data, filename, caption)`, `MIME`, `mime_of`.
- document: read-only extraction in memory (text/code/web/Office via defusedxml + zip-bomb limits `MAX_UNZIPPED=50MB`, `MAX_MEMBERS=2000`; PDF text via pypdf, scanned PDF → page images `pdf_page_images`); chunked summarize (`CHUNK`, `MAX_CHARS=30000`). `Unsupported`, `is_supported`, `ext_of`, `LEGACY_EXT`.
- keywords: `extract_keywords(llm, material, recent)` → (kw list ≤8, gist); `filter_keywords`, `_NOISE_KW`.

### ocr.py
`OcrWorker(dc, index, send)`; requests the App to OCR; queue of mirror urls (`submit`, `submit_entries`, `scan_index`), `rewrite` queue for legacy records, `process(mirror_url)`.
- Image records: `is_image_record` (type image; album only if filename has `IMAGE_EXT`; files by extension; not origin extra). `match_attachments(entries, attachments, content)`: exact name → normalized name (`_norm`: alnum only, strip SPOILER_) → exclude `📎` missing names (`_missing`) → order when counts match.
- Link images: `is_link_image_record` (rec.img set, not an image record) → `_link_images` sends each URL directly to the App, joins texts into `ocr`.
- `_ask(url, name)` → result dict or None (timeout `TIMEOUT=120`); pacing `PACE_SEC=0.4`; backoff for transient download errors (`retry`, `retry_after`); non-transient errors retried until `MAX_TRIES=3` (`_fails`) then `（無法取得圖片）`.
- `needs_ocr`, `useful`, `clean` (Luhn card masking → `＊＊＊＊1234`). No failure log file. `scan_index()` (called at startup and by `cycle()` at every start of the `ane` lane, i.e. schedule or run-now) queues every record with no `ocr`, truncated legacy `ocr`, or `ocr == （無法取得圖片）` (image and link-image records) into `_retry_keys` and resets its `_fails`, so previously failed/unfinished images are redone each round; they end up `（無法取得圖片）` again only after `MAX_TRIES` failures or when no mirror attachment matches. `_clear_stale` clears ocr on album videos. Days with new text and an existing daily summary → `mark_days` (RECAP_DIRTY).

### ipc.py
Enabled when `INBOXRELAY_IPC=1`. JSON lines; logs go to stderr.
- App → core (stdin): `{"type":"lane","lane":"llm|ane","action":"run_now|pause|resume"}`, `{"type":"hello","ocr":true}` (ane available), `{"type":"ocr_result","id":N,"text":"…","ms":N}` or with `"error","retry","retry_after"`, `{"type":"recap_reset"}`, `{"type":"stop"}`; legacy `recap_now|pause|resume`.
- core → App (stdout): `{"type":"status", paused, tasks[{label,done,total,detail,line,progress}], backlog, probe_pending, summarized_until, recap_enabled, recap_phase, recap_time, next_recap_hours, ai_status, chat_title, rss_mb, running, lanes{llm|ane:{label,mode,state,describe,queued,backlog,next_start}}}` every 2 s on change; `{"type":"ocr_request","id":N,"url":"…","name":"…"}`.
`IpcServer(act, relayer, get_recapper, tg, cfg, ai_status, stop)`, `lanes_status()`, `emit()`.

### recap/runner.py / recap/daily.py
- `RecapChannel`: posts in #總結 with marker line `DAILY|YYYY-MM-DD`, `MONTHLY|YYYY-MM`, `YEARLY|YYYY` (optional ` i/n` for chunks, may be prefixed `-# `); `load`, `publish` (edit in place), `remove`, `exists`, `has`, `first`, `keys`, `forget_message`.
- `Recapper.cycle()`: wait `enricher.drained` (poll `DRAIN_POLL_SEC`) → `tick()` publishes in timeline order (`timeline_key`): days with data minus existing DAILY, dirty days (RECAP_DIRTY), month after its last day, year after December. Offline error → `lane.go_offline`. `summarize_day/month/year`, `reset_all`, `add_note` (RECAP_NOTES), `trigger_now`, `set_schedule`, `month_ready`, `year_ready`, `unenriched`. `TYPE_LABEL` maps record types.
- daily: `Item(time, type_label, text, mirror)`, `build_material`, `build_prompt`, `link_citations` (`[↗n](<url>)`), `split_message`.

### search/engine.py / search/channel.py
- engine: `parse_query` (LLM SEARCH_PARSE → date range + terms) → `collect`/`select` candidates from KwIndex (excerpt, filename, kw, gist, ocr window) + DAILY summaries (≤ `MAX_CANDIDATES=120`) → SEARCH_ANSWER → `format_reply` (dates, no message ids).
- channel: `SearchChannel` (#搜尋 live when SEARCH_LIVE; 搜尋測試 searches AI-test content); follow-ups via reply chain (`MAX_TURNS=3`, `TURN_CHARS=600`), SEARCH_FOLLOWUP routes general vs search.

### chat.py
`ChatChannel` for DC_CH_CHAT. Root message → thread (`thread_title`); per-thread lock; history = thread messages (`merge_roles`, `trim_history` by token budget from LLM_CONTEXT − output − `SAFETY_TOKENS`, chars↔tokens ratio `_tpc` calibrated from usage); images (latest `MAX_IMAGES=4`, ≤`MAX_IMAGE_BYTES`), documents (`DOC_CHARS=20000`), links (`LINK_CHARS=6000` via fetch_text).
Commands: `THINK_PREFIX="/思考"`, `COMPACT_CMD="/compact"`. Footer `USAGE_TAG="-# 🧮"` (上下文 used/ctx tokens). Auto-compact at `chat_compact_pct`: `compact()` streams CHAT_COMPACT with text progress bar (`progress_bar`, ▰▱, edited every 2 s, deleted after) → summary messages starting `SUMMARY_HEAD="🗜️ **對話摘要"`; `history()` resumes after the last summary.
Prompt/memory in DC_CH_CHAT_PROMPT (latest `#提示詞` / `#記憶`, default published if missing, `PROMPT_LIMIT=1900`); memory = `MemoryStore(apply_to_llm=False, upgrade_prompt="CHAT_MEMORY_UPGRADE")`, refresh flag `chat-memory-rules-v2`. Model marks memorable lines with `MEMO_TAG="〔記憶〕"` (`split_memos`, stripped from replies); `EXPLICIT_RE` (記住|幫我記|記得我) → correction + immediate upgrade; `SOON_RE` (以後|之後|下次|從現在開始|不要再|別再|一律) + memo → immediate; else after `IDLE_UPGRADE_SEC=600` idle. Uses `llm.chat_messages` (interactive priority).

### feedback.py
`FeedbackChannel` (DC_CH_NOTIFY, owner/editors only): FEEDBACK_ROUTE JSON → `fix_daily` (RECAP_NOTES + rewrite day + month), `fix_monthly`, `memory` (`memory_note` imperative rule → `add_correction` + `upgrade`), `other`. `parse_route`.

### aitest.py
`AiTestChannel` (DC_CH_AI_TEST): analyze any message like the pipeline; `!總結 [date|全部]` preview; `!說明`.

### importer/
- reader.py: streaming `result.json` (ijson) → `HistMsg(tg_msg, dt, text, type, rel_path, file_name, missing_reason)`; photo file name = basename of export path; missing → `file_<id>`; `to_bot_chat_id`.
- packer.py: `Packer`/`pack_all` → `Packet`s (same-day text merged ≤2000; attachments one per message; date header first of day).
- job.py: `ImportJob` (plan, dry-run, checkpoints, IMPORT lock, `MIRROR_INTERVAL`), records origin=`import`.
- extra.py / extra_job.py: `import-extra` → `DayDoc(day, kind, icon, name, body, …)` from 財政部 invoice CSV (`is_invoice_csv`, `invoice_docs`) or JSONL (`parse_jsonl`: day, kind, icon, name, body, title?, excerpt?); one post per day+kind in DC_CH_EXTRA; KW record origin=`extra`, tg_chat=0 (`EXTRA_CHAT`), type `invoice`/kind; changed days → RECAP_DIRTY. Personal-format converters must not be added to the repo.

### reset.py
`run_reset(...)`: deletes mirror messages, bot posts in keywords/recap/screenshot/extra, `STATE_TAGS=("STATE","IMPORT","LIVE","RECAP_DIRTY","RECAP_NOTES")`; flags `--memory --chats --prompts --all`. Telegram untouched. (App `recap_reset` IPC is different: it calls `Recapper.reset_all()` — clears #總結 and re-summarizes.)

### memguard.py / statusbar.py
- memguard: RSS every minute; > soft → clear caches; 3× > hard → exit (App restarts) or warn.
- statusbar: terminal bottom bar via DECSTBM; disabled when not a TTY or `STATUS_BAR=false`.

### tests/
unittest, run: `python -m unittest discover -s relay-core/tests`. Files: test_activity, test_chat, test_enrich, test_extra, test_formatting, test_importer, test_lanes (incl. offline), test_link_image, test_m4, test_ocr, test_ocr_message, test_recap, test_records_album, test_reset, test_reverse, test_search.

## 4. App (app/*.swift, macOS 14+, built with swiftc, no Xcode project)

| File | Role |
|---|---|
| InboxRelayApp.swift | `@main InboxRelayApp`: MenuBarExtra + main window; launch at login via `SMAppService.mainApp` |
| CoreController.swift | Launches bundled core (`Contents/Resources/core/run.sh`) with env from settings + `INBOXRELAY_IPC=1`; restart with backoff; parses status JSON (`CoreStatus`, `CoreTask`, `LaneStatus{label,mode,state,describe,queued,backlog,next_start}`); lane actions; launch arg `--instant` → once status shows `llm` and a connected `ane` lane, sends `run_now` to both; `handleOCR` (calls OCRService, replies `ocr_result` with retry info); `recapReset`; in-memory `logs` |
| OCRService.swift | `recognize(urlString:completion:)`: ephemeral URLSession (no cache/cookies), HTTP status check (non-2xx → retry with Retry-After), `VNRecognizeTextRequest` (accurate, zh-Hant + en-US, language correction), Neural Engine first, then CPU/GPU, then a re-render to standard sRGB 8-bit RGBA (long side ≤ 4096; works around `CRImageReaderError` on unusual formats); serial queue, image in memory only |
| SettingsStore.swift | `SettingCatalog.fields` (key = env name, label, kind text/secret/toggle/time/choice/number, section 連線/頻道/外部 LLM 設定/排程/功能/…); secrets in Keychain; others in UserDefaults or synced folder `settings.json` (optional secrets there); first run imports `.env` |
| Views.swift | Menu content (lane cards: colors running green/teal, waiting blue, offline red "LLM 離線", paused orange; run/pause buttons), dashboard, settings (reset button with confirmation, Cancel default), log view |
| core_run.sh | Bundled launcher: venv in `data_dir/venv` (requirements hash stamp), `INBOXRELAY_ENV=data_dir/.env`, stdout reserved for JSON |

tools/linkshot.swift: WKWebView with `WKWebsiteDataStore.nonPersistent()`, loads URL, settles, full-page snapshot → JPEG on stdout; `--insecure` bypass prints `CERT_BYPASSED`. tools/make_icon.swift: generates app icon at build.

## 5. Local files (data_dir = ~/Library/Application Support/InboxRelay)

| Path | Written when |
|---|---|
| venv/ | first launch / requirements change |
| .env | optional (App passes env directly) |
| instance.lock | while core runs |
| link-refresh-v1, memory-rules-v2, chat-memory-rules-v2 | one-time migration flags (content `done`) |

## 6. Discord data conventions

| Where | Message | Producer |
|---|---|---|
| #投遞箱鏡像 | mirror posts (date header, text, files, 📎 lines); AI reply under mirror (`**🔗 url**`, `**🖼️ name/url**`, `**📄 name**` + `> ` lines, `📸 截圖：[↗1](<url>)`) | relay.py, enrich/pipeline.py |
| #關鍵字庫 | `KW|v2` records, `OCR|chat|msg` texts, activity message | records.py, kwindex.py, activity.py |
| #資料庫 | STATE, IMPORT, LIVE, RECAP_DIRTY, RECAP_NOTES, `#記憶 vN｜…`, `#提示詞 NAME vN｜…` | state.py, memory.py |
| #總結 | `DAILY|…` / `MONTHLY|…` / `YEARLY|…` posts | recap/runner.py |
| 連結預覽與進度 | `📸 when｜url` + image `preview.jpg`; activity message | enrich/screenshot.py, activity.py |
| AI 對話 | threads; footer `-# 🧮`; `🗜️ **對話摘要` | chat.py |
| AI 對話提示詞與記憶 | `#提示詞`, `#記憶` | chat.py |

## 7. Build & run

- Dev: `./run.sh [subcommand]` (uses repo `.env`, `.venv`).
- App: `./install_app.sh` (build → replace /Applications/InboxRelay.app → open). Build log `app/.build.log`.
- Tests: `python -m unittest discover -s relay-core/tests`.
- Python deps: `relay-core/requirements.txt` (python-telegram-bot, discord.py, ijson, openai, trafilatura, lxml_html_clean, python-docx, openpyxl, python-pptx, defusedxml, charset-normalizer, pypdf, pypdfium2, psutil).
