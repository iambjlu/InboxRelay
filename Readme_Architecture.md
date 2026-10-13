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
- Two work lanes (`lanes.py`): `llm` (external LLM background work) `ane` (local OCR via the App) and `shot` (link screenshots, own schedule; exists only when LINK_SCREENSHOT + DC_CH_SCREENSHOT + tools/linkshot are available, otherwise removed from `LANES`). Interactive features (search, chat, feedback, AI test) bypass lanes.

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
├── tools/                    linkshot.swift (+ Info.plist ATS exception), make_icon.swift, localocr.swift (CLI: `localocr <paths…>|-` → one JSON line per image {path,text,error,ms}; same Vision settings as OCRService, tall/wide screenshots are tiled; compile with `swiftc -O tools/localocr.swift -o <out>`; not bundled in the App)
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
| DC_CH_SCREENSHOT | ch_screenshot | None | 連結預覽與進度 (screenshots) |
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
| LLM_WAIT_SHOT | llm_wait_shot | true | `llm` lane is blocked (`Lane.blocked_by`, state `waiting`, describe "等連結截圖做完") while the `shot` lane is `running` with a non-empty queue; `ShotWorker.on_drained` wakes the llm lane. Needs LINK_SCREENSHOT |
| SHOT_SCHEDULE / SHOT_START / SHOT_END | shot_schedule / shot_start / shot_end | always / 03:30 / 08:00 | `shot` lane schedule (link summary stays on the `llm` lane) |
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
Diagnostics: `kill -USR1 <core pid>` makes the core print the stack of every asyncio task, the lanes' state, the `_Gap` value, the OCR queue/pending writes/backoff/last failure reason and the analysis backlog to the log (`dump_tasks`, registered in `run()`); only when the diagnostic mode is on (`diag.enabled`) is it additionally written to `tasks-dump.txt` in the data dir. Diagnostic mode (`diag.py`, default off): logs are only shown (the App's 日誌 tab keeps the last 500 lines in memory, the terminal prints them) and nothing is written to disk; the App's per-machine preference `AppPrefs.diagnosticKey` (`diagnosticLogging`, settings 「一般」 toggle 「診斷模式（把日誌寫入檔案）」) turns on `DiagnosticLog` (every log line appended to `~/Library/Logs/InboxRelay/diagnostic.log`, rolled to `diagnostic.1.log` at 5 MB, ≈10 MB max) and is sent to the core with IPC `{"type":"diag","on":bool}` (at launch after `hello` and whenever toggled), which sets `diag.enabled`.

Entry point. Subcommands: (none) live core; `--check`; `import --path [--from --to --dry-run --append-out-of-order]`; `import-extra --path [--from --to --dry-run]`; `reset [--yes --memory --chats --prompts --all]`; `recap [--date|--month|--year]`. Exit codes: 0 ok, 2 config/permission, 3 import running, 4 already running on this machine, 5 running on another host.
`run(check_only)` wiring order: `lanes.setup` → Discord login + permission check → LLM ping (failure → `lanes.get("llm").go_offline`) → `MemoryStore` + `PromptStore.load` → `mem.refresh_once("memory-rules-v2", …)` → Telegram → `Relayer`/`Enricher`/`Prober` → recap (`Recapper`, `run_lane(llm, cycle=recapper.cycle)`) → IPC (`OcrWorker`, `enricher.on_link_images = ocr.submit`, `run_lane(ane, cycle=ocr.cycle)`) → background `_refresh_links` (waits `enricher.drained`, then `enrich.refresh.refresh_links`) → `poll_new_records` every 300 s → `queue_backlog` (un-enriched records: recent live → probe, else enrich).
Orphan guard (`_watch_orphan`, IPC mode only, started in `main()` before any slow startup work): a daemon thread polls `getppid()`; when the App is gone it sends SIGTERM to itself and `os._exit(0)` after 30 s, because during startup the core neither reads stdin nor reaches `stop.wait()`. Shutdown: after the stop signal (IPC `stop` or stdin EOF) a daemon `threading.Timer(25 s)` calls `os._exit(0)` if graceful shutdown hangs, so no orphan core keeps `instance.lock`. `install_app.sh` waits for old core processes (matched by the bundle path), force-kills leftovers, and after launch verifies App + core are running (exit 1 otherwise).
Functions: single_instance, setup_logging, install_console (terminal keys n/p/r/s/h), notify, online_message, status_bar_lines, queue_backlog, poll_new_records, run_recap, run_reset_cmd, run_import_extra, run_import.

### config.py
`Config` dataclass, `load()`, `load_env_file()` (does not override existing env), `REQUIRED`. `channel_map()` / `channel_map_with_test()` for permission checks. See §2.

### paths.py
`data_dir()` → `INBOXRELAY_DATA` or `~/Library/Application Support/InboxRelay` (created on demand).

### lanes.py
`Lane(name, label, mode, start, end, tz)`; `MODES = always|daily|window|manual`; `ALWAYS_CHECK_SEC = 600`.
State fields: `open`, `manual`, `paused`, `available` (ane: App connected), `offline` (llm unreachable), `next_start`, `close_at`. `state` → `unavailable|paused|offline|running|waiting`. `allowed()` = available ∧ ¬paused ∧ ¬offline ∧ (always ∨ open) ∧ ¬`blocked` (`blocked_by` callable, set for llm when LLM_WAIT_SHOT).
Methods: `gate()` (await until allowed), `pause()`, `resume()`, `run_now()`, `go_offline(why)` (ends current round; work kept; retried next start or after 600 s in always mode), `describe()`.
`run_lane(lane, drained, cycle=None)`: loop {wait start/trigger → clear `offline` → run `cycle()` then wait `drained()` → stop on close_at / done / offline}. Globals `LANES`, `setup(cfg)` creates `llm` (start=RECAP_TIME, end=LLM_END) and `ane`, `get(name)`.

### activity.py
Progress tracking for background work. `Task` (label, done, total, detail, item steps, llm_frac; `line()` estimates 「剩約」 from the progress made in the last `ETA_WINDOW_SEC=600` s, and shows `停滯中` instead when there was no progress for `STALL_SEC=300` s), `Activity` (tasks, Discord progress message `STATUS_HEAD` in #投遞箱鏡像 after `DISCORD_AFTER_SEC=10`; when the text changes (at most every `DISCORD_EVERY_SEC=15`) a new message is posted at the bottom of the channel and the previous one deleted (never edited, so it is always the latest message); deleted when idle; `cleanup_old` at startup removes leftovers in the mirror, 連結預覽與進度 and #關鍵字庫 channels (bot messages in the mirror without a record are ignored by delete-sync and reverse-sync); `status_lines()` adds lane lines incl. "🔌 外部 LLM 連不上"). Module API: `init, start(label,total,background), finish, bind, bound, step, begin_item, gate(lane_name="llm")`.

### llm.py
Formatting: Discord has no Markdown tables, so every model call carries `prompts.FORMAT_RULE` (code-level, not in the editable #提示詞: no tables, use `- ` bullets unless the task specifies a format) — appended to the system prompt in `LLM._system` and in `ChatChannel.system_prompt` — and every returned text goes through `mdtable.tables_to_bullets` (in `chat`, `chat_messages` incl. streaming): a table (header row + `---` separator row, outside code fences) becomes bullets: 2 columns `- A：B`, 3+ columns `- 第一欄：表頭 值；表頭 值` (empty cells skipped).
`LLM(base_url, model, api_key, image_support)` over `AsyncOpenAI(timeout=300, max_retries=1)`; single-flight `Semaphore(1)`.
- `chat(prompt, data=None, image=None, image_mime, max_tokens, thinking, use_memory)` → str. System = `GUARD` + `MEMORY_WRAP(memory)`. Background calls (bound activity task) stream for progress and yield to interactive callers (`_interactive_waiting`). `<think>` stripped.
- `chat_messages(messages, max_tokens, thinking, temperature, progress=None)` → (text, usage) for multi-turn chat; `progress(n_tokens)` enables streaming.
- `ping()` → model ids. `memory`, `memory_version` set by MemoryStore.
- `test_connection(base_url, model, api_key)` → `{ok, message, ms}`: standalone client (timeout 20 s, no retry) for the settings-page test; only calls `GET /models` (HTTP 200 = endpoint and key OK; model name missing from the list → not ok); never generates text; maps 401/connection/status errors to readable messages. Does not touch the running `LLM`.
- `strip_reasoning(text)`: applied to every `chat`/`chat_messages` result — removes `<think>…</think>`, keeps what follows a lone `</think>`, and returns `""` when the reasoning was cut off (only `<think>`, or a body starting with `Here's a thinking process` / `Thinking Process` / `思考過程`) so reasoning is never shown as an answer; callers treat `""` as "no answer".
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
enriched: yes|no|ocr   (ocr = the analysis ran after the images' text recognition had finished; yes = older analysis that may not have used OCR)
```
`KwRecord` dataclass fields: tg_chat, tg_msg, origin, date, type, mirror, src, filename, fid, reply (AI reply url), shot (screenshot post urls, space-separated), kw, gist, excerpt, enriched, enriched_ocr, ocr, img (image URLs found in the text, space-separated). Key = (tg_chat, tg_msg); extra imports use tg_chat=0.
OCR text lives in a separate message `OCR|tg_chat|tg_msg\n<text>` (≤ `OCR_ROOM=1960`). Constants: `PREFIX="KW|v2"`, `OCR_PREFIX`, `OCR_REF="見 OCR 訊息"`, `OCR_MARKERS=("（無文字）","（無法取得圖片）")`, legacy `OCR_NL=" ⏎ "`.
Functions: format_kw, parse_kw, parse_key, from_dict, parse_record, ocr_message, parse_ocr_message, flat_ocr, make_excerpt(200), one_line, mirror_texts(recs, content) (per-record full text cut from the mirror message: drops `#`/`-#` header & src lines and `📎` lines, anchors on each record's excerpt start), parse_message_url.

### kwindex.py
`KwIndex`: in-memory map key → `KwEntry(msg_id, rec, ocr_msg, legacy)`; loads whole #關鍵字庫 at startup, `poll_new()` incremental; `update(key, **changes)` edits the record message (skipped when the formatted text is unchanged and the entry is not legacy) and syncs its OCR message (`_sync_ocr`); `by_mirror(url)`, `get`, `delete` (also deletes OCR message). All edits go through `_edit` (one `asyncio.Lock`, at least `_Gap.current()` s apart = `max(EDIT_GAP_SEC=1.0, extra)`; a handler on the `discord.http` logger raises the extra gap ×1.5+0.3 (max `MAX_GAP_SEC=4`) only for a PATCH 429 on the #關鍵字庫 channel (channel id parsed from the warning, registered by `KwIndex`), at most once per edit (discord.py's retries of the same edit do not stack), and the extra gap halves every `HALF_LIFE_SEC=20` s without further 429s) so the OCR and shot lanes updating records at the same time do not hit Discord's per-channel message-edit rate limit (429).

### state.py
#資料庫 fixed records (first line tag, then `key: value`, edited in place): `STATE` (host heartbeat; `HOST_STALE=3 min`), `IMPORT` (import lock/checkpoint; `LOCK_STALE`), `LIVE`, `RECAP_DIRTY` (`DirtyDays`: days to re-summarize), `RECAP_NOTES` (per-day user notes). `DbRecord`, `this_host`, `other_host_active`, `import_lock_active`.

### formatting.py
Pure helpers for mirror message text: date headers (`header`, `parse_header_date`, `WEEKDAYS`), `build_mirror_content`, `build_live_message`, `oversize_line(name)` → `📎 name（過大未搬移）`, `truncate`, `supergroup_src_url`, `DISCORD_LIMIT`.

### tg_intake.py / album.py
`TelegramIntake`: polling, group migrate handling, `classify(msg)` → (type, `AttachmentRef(file_id, file_name, file_size, file_unique_id)`, text); photo name `photo_<msg_id>.jpg`; album pieces (media_group_id, buffered 2 s by `AlbumBuffer`) get `type="album"`. Emits `RelayItem(pieces: list[TgPiece])` to a queue. Edits → `_handle_edit`.

### relay.py
`Relayer`: consumes RelayItems; dedupe by index; attachments grouped by `plan_groups` (≤10 files and ≤ size limit per message; a `.heic/.heif` attachment is always a group of its own), downloaded per group into memory; `_add_jpeg_copy` (in `handle` and `_collect_files`, i.e. new messages and re-uploads on edit) attaches a JPEG copy `<original name>.jpg` of every HEIC/HEIF to the same Discord message (`enrich.image.heic_to_jpeg`: full resolution, EXIF orientation, quality 92→55 then downscale until it fits `limit − HEIC size`; skipped when less than `MIN_ROOM=300KB` remains or conversion fails; only for future messages, nothing is backfilled); the copy is recognised by `is_converted_copy` and ignored by `Enricher.process` and `ocr.match_attachments` so images are not analysed/OCRed twice; oversize/failed → `📎` lines; writes KW records; hands off to `Prober` (live) then `Enricher`; edit sync (re-upload when fid changes), delete sync; `TextCache` (full text for live messages, `TEXT_CACHE_MAX`). Attributes `enricher`, `ocr`, `prober`, `dirty`, `index`, `text_cache`.

### probe.py
`Prober`: after `PROBE_DELAY_SEC`, `copyMessage` original to probe chat; "message to copy not found" → delete mirror + record; else allow enrichment.

### dc_output.py
`DiscordOut`: client wrapper (`send(…, view=…)` attaches buttons); handler registries `add_message_handler`, `add_edit_handler` (raw message edit), `add_delete_handler` (message delete), `add_thread_delete_handler(handler(thread_id, parent_id))` (raw thread delete); `channel(id)`, `send(channel_id, content, files=[(name, bytes)], retries=3)`, `send_file`, `fetch_by_url`, `delete_by_url`, `load_kw_keys`, `filesize_limit`, permission check (`view_channel, send_messages, read_message_history, attach_files, embed_links, manage_messages`), `add_message_handler`.

### dc_reverse.py
`ReverseSync`: messages posted by users in #投遞箱鏡像 → sent to Telegram (photo ≤10 MB, files ≤50 MB, caption ≤1024); record origin=`dc`, mirror = the user's own message; edits/deletes mirrored.

### enrich/pipeline.py
`Enricher(cfg, dc, llm, index, text_cache, memory)`; one job = `EnrichJob(keys, mirror_url)`.
Queue: `asyncio.Queue` + `_front` deque (jobs returned after LLM offline, processed first); `backlog`, `drained`; `submit()` dedupes by mirror url. `on_link_images: callable(mirror_url)` (set to `OcrWorker.submit`).
`run()`: `activity.gate()` + `lanes.get("llm").gate()` → `process(job)`; on `is_offline_error` → `_undo_posted()` (delete reply/screenshot posts of this job), push job to `_front`, `lane.go_offline()`; other errors logged and job dropped (record stays enriched=no; re-queued at next startup).
Per-record material: each record's keywords/gist use only ITS OWN part summaries (`Enricher._own_summaries`: link parts whose URL is in the record's text, attachment parts mapped to the record by `ocr.match_attachments` / normalized filename; `part_owner` runs parallel to `parts`) — never the whole mirror's summaries — so the photos of an album do not pick up each other's people and events; the image caption uses only that photo's record text and OCR (matched with `match_attachments`, so renamed files still get their OCR) and says the OCR text has no left/right position. `prompts.CHAT_RULE` (code-level, appended to the IMAGE prompt in `summarize_image`): in a chat screenshot the right-side bubbles are the user, the left side the other party, label every speaker, never mix speakers or other images. `main.queue_album_redo` (flag `gist-per-image-v1`, once): already-analysed mirrors with ≥2 records are re-analysed and their days marked dirty for re-summarising. `process(job)`: first `ocr_worker.ensure(mirror)` (image analysis always gets the OCR text as reference: caption + keyword material); at the end `enriched_ocr` = no pending OCR for the mirror (or no IPC/App at all); when a new reply is posted the previous reply messages of those records are deleted (re-analysis replaces the old summary; if there is nothing new the old reply link is kept). `main.queue_ocr_redo` queues, at startup, every image record that is `enriched` but not `enriched_ocr` and already has OCR text. texts = `text_cache` → `records.mirror_texts` of the mirror → excerpt; every URL is handled (no cap): if the `shot` lane is `allowed()`, first `shot_worker.ensure(mirror)`; `_shot_posts(keys)` maps URL → successful screenshot post (text only); then per URL sequentially `probe_link` → screenshot bytes read only for that URL → `summarize_probed` (image URL → 🖼️ part; unreadable page → preview+screenshot fallback) → both released before the next URL. Then attachments (`summarize_image`, `summarize_document`, ≤ `MAX_FILE=20MB`) → one reply under the mirror (`daily.split_message`, plus a `📸 截圖` line from `success_links(rec.shot)`) → per key `extract_keywords` + `memory.observe` → `index.update(kw, gist, enriched=True, reply, img)`.

### enrich/link.py
`find_urls` (`URL_RE`, ASCII only). `fetch_link(url, want_image)` → (text, kind, bytes) with kind `article|og|file|image|""` (HTML ≤ `MAX_BYTES=3MB`, image ≤ `IMAGE_MAX=20MB`, non-HTML only headers read → `file_link_note`). `fetch_text(url)` → (text, kind) (used by chat.py). `probe_link(url)` → (text, kind, bytes, err) with kind `private` when blocked by PRIVATE_URLS. `embed_preview(message, url)` → Discord embed (site/author/title/description). `summarize_probed(llm, url, probed, preview, shot, caption, describe_image)` → `LinkResult(title, summary, image, failed)`; for kind `article`, `fetch_link` prefixes the body with `網頁標籤：<og:title/description>` (so body mixed with nav/ads can be cross-checked) and the summary also gets the Discord embed `網頁預覽` when it differs, plus a hint that the body may contain navigation text; fallback uses prompt `LINK_FALLBACK` and appends `（根據網頁標籤、網頁預覽、截圖）`. `summarize_link(llm, url)` → str (aitest). `sniff_mime`, `human_size`, `UA`.

### enrich/screenshot.py
Runs `tools/linkshot <url> --timeout --settle 5 --width 1280 --max-height 4000 [--insecure]`; image from stdout (JPEG/PNG ≤ `MAX_PNG=8MB`); non-persistent WebKit store. `capture`, `capture_with_reason`, `capture_many(urls, label, failures)` (AI test channel). Failures are only logged to the core logger (no file). `ALLOW_PRIVATE`, `IGNORE_CERT`, `CERT_WARN`, `is_public_url`. `try_capture(url, tries=1)` → (png, why) (DNS check → capture; `NetworkDown` when a non-permanent failure coincides with no connectivity); `post_one(dc, channel, url, when, back_url, tries=1)` → (post link, ok, why) (uses `try_capture`; the failure notice carries a `🔄 重試截圖` button, `enrich/shotbutton.RetryView`): DNS check first (`_host_resolves`; unresolvable → reason 網域不存在), otherwise one capture (`TIMEOUT` 20 s, `SETTLE=3`); success posts `📸 when｜url` + image, failure posts `📸 無法截圖｜when｜<url>` + `-# 原因：…`. Failures are not retried here; if the failure is not permanent (`is_permanent`: 內網／不是 http／圖片太大) and `network_ok()` (TCP to www.google.com:443) fails, `NetworkDown` is raised and no notice is posted. `RETRY_MARK="?"` (`?<n><link>` = failure n, will be retried), `FAIL_MARK="!"` prefixes failure-notice links in `rec.shot`. (`post_screenshots` removed.)

### enrich/shotworker.py
Retry button: `enrich/shotbutton.py` — `RetryView` (persistent view, fixed `custom_id`, `handler` is `ShotWorker.retry_notice`; registered in `main` with `dc.client.add_view`), `parse_notice(content)` → (when, url, mirror link) read from the notice text. `ShotWorker.retry_notice(msg)` (holds `ShotWorker._lock`, which `run()` also holds while processing a mirror): `try_capture` → on success posts the screenshot, replaces the notice's token (`!`/`?n` link) in every record of that mirror with the new link, deletes the notice, refreshes the AI reply's `📸 截圖` line and the mirror arrows (`sync_arrows`), and answers `✅`; on failure rewrites the notice's `-# 原因：` line and answers `❌ 還是失敗：…` (records unchanged); `NetworkDown` answers that the network is down. `add_retry_buttons()` (flag `shot-buttons-v1`, in `_shot_maintenance`, 1 s per edit) adds the button to the existing notices referenced by `!`/`?` tokens.
Tokens in `rec.shot` (`parse_token`): success link / `?<n><link>` retryable failure / `!<link>` final failure / `-` skip. A `?` token is due again (`retry_due`, counted in `needs_shot`) `RETRY_AFTER_SEC=6h` after the notice message time (snowflake) until `RETRY_MAX=3` failures, then becomes `!`; a retry deletes the old notice and posts the new result; `?0` is due immediately. `NetworkDown` during `process` saves the finished URLs, re-queues the mirror at the front and `_wait_network` pauses the worker (log only, check google.com every 30 s up to 5 min) until the network is back. `retry_old_failures()` runs once (flag `shot-retry-v1` in data dir): every `!` token whose notice reason is not permanent (or whose notice is gone) → `?0`. Mirror arrows: after `process` (and via `sync_arrows(mirror)`), every line of the bot's own mirror message that contains a URL with a successful screenshot gets a small-text line below it `-# [↗ 截圖](<screenshot post>)` (`add_arrows`: old arrow lines are stripped first so it is idempotent; header/`-#`/`📎` lines and code blocks are skipped; skipped when > 2000 chars; user-posted mirrors are not edited; `mirror_texts` ignores `-#` lines so excerpt anchoring is unaffected; a Telegram-side edit that rewrites the mirror drops the arrows until the next `sync_arrows`). `backfill_arrows()` (flag `shot-arrows-v1`, runs after `retry_old_failures` in the `_shot_maintenance` task, 1 s per edit, gated by the shot lane) adds arrows to all existing mirrors. URL kind is decided by `link.probe_kind` (headers only, no page download/extraction): `private` / `binary` (`classify_content(ctype, length)`: anything whose type is not text/*, image/*, PDF or contains json/javascript/yaml/toml/sql/html/xml — e.g. zip, video, octet-stream — or > 100 MB) → SKIP; everything else, including PDF, text and image URLs, is screenshotted (linkshot renders them). `reset_skipped()` (flag `shot-skip-v3`) once removes every `-` token from records whose excerpt has URLs so those URLs are re-judged (other tokens stay); `retry_unrenderable_failures()` (flag `shot-retry-v2`) once turns `!`/`?` failure notices whose reason is `不是可截圖的網頁（<json/javascript/yaml…>）` into `?0` (retry now). Both run at startup in `_shot_maintenance` after the lane's `scan_index`, so they become backlog on the next launch. If neither `text_cache` nor the mirror yields the full text and the excerpt is truncated, a warning is logged (links after 200 chars may be missed). `ShotWorker(cfg, dc, index, text_cache)` runs the `shot` lane (`run_lane(shot, drained, cycle=shot.cycle)`). `rec.shot` holds one token per URL in order: success post link / `!`+failure-notice link / `SKIP="-"` (private URL or a file the browser cannot preview such as zip/video/installer (`link.probe_kind` → `binary`); also a lone `-` for a long message without URLs so its mirror is not re-read). `needs_shot(entry)` (no Discord reads): not origin extra and fewer tokens than URLs in `text_cache`, else in the excerpt; a truncated excerpt (`…`) with no tokens also counts. `scan_index()` runs at startup and every lane start (schedule/run-now), so backlog is retried each round. `submit(mirror, front=False)` is called from `Enricher.submit`; `ensure(mirror, timeout=SHOT_WAIT_SEC=240)` lets the Enricher jump the queue and wait (`_release`). `process(mirror)`: fetch mirror → per record URLs from `text_cache` / `records.mirror_texts` / excerpt → `_covered(old tokens)` reuses existing posts (URL read from each post's first line, no re-shoot) → for each remaining URL `probe_link` (image/file/private → `-`) else `screenshot.post_one` → `index.update(shot=…)` (unmatched old tokens kept at the end so delete sync still removes them) → `_update_reply` replaces the reply's `📸 截圖：` line with all successful shots (≤2000 chars). Only a mirror read failure returns False (backoff 30 s doubling to 900 s). `success_links(shot)`, `shot_line(links)`. Screenshots stay in memory.

### enrich/refresh.py
One-time backfill (flag `link-refresh-v1`): for records with `reply` and a URL in the excerpt, fetch the reply message, find blocks `**🔗|🖼️ url**` + `> …` lines whose body `needs_refresh` (無法讀取 / 僅根據網頁標題與描述 / 檔案下載連結+image/), redo with `summarize_probed` (old screenshot from `rec.shot` or a fresh capture, not posted), edit the reply (`join_fit` ≤2000), set `img` and notify OCR. Offline → `go_offline`, wait lane, retry same item. Functions: needs_refresh, parse_segments, join_fit, refresh_one, refresh_links.

### enrich/image.py / document.py / keywords.py
- image: `prepare_image(data, filename)` → (bytes, mime) | None (`detect_mime` by header; JPEG/PNG/WebP/GIF as is, anything else e.g. HEIC/TIFF/BMP converted to JPEG in memory with Pillow + pillow-heif, longest side ≤ 2048); `summarize_image(llm, data, filename, caption)` (uses `prepare_image`; an `openai.BadRequestError`, e.g. undecodable image, returns a `（圖片無法解析…）` note instead of failing the whole message), `MIME`, `mime_of`.
- document: read-only extraction in memory (text/code/web/Office via defusedxml + zip-bomb limits `MAX_UNZIPPED=50MB`, `MAX_MEMBERS=2000`; PDF text via pypdf, scanned PDF → page images `pdf_page_images`); chunked summarize (`CHUNK`, `MAX_CHARS=30000`). `Unsupported`, `is_supported`, `ext_of`, `LEGACY_EXT`.
- keywords: `extract_keywords(llm, material, recent)` → (kw list ≤8, gist); `filter_keywords`, `_NOISE_KW`.

### ocr.py
`OcrWorker(dc, index, send)`; requests the App to OCR; results are written to #關鍵字庫 in the background (`_write`, at most `WRITE_AHEAD=30` ahead of Discord, `flush()` after each mirror; `backlog`/`drained` include pending writes) so the Vision engine is not idle while Discord edits are paced; `ensure(mirror, timeout=WAIT_SEC=1800)` (used by the Enricher before analysing a mirror): when the `ane` lane is `running` and `pending_for(mirror)`, moves the mirror to the front (`submit(front=True)`) and waits until it is processed and written (`_release`), otherwise returns at once; `on_text(entry)` is called when text was found for a record whose analysis ran without OCR (`enriched and not enriched_ocr`) → the Enricher re-analyses the mirror; queue of mirror urls (`submit`, `submit_entries`, `scan_index`), `rewrite` queue for legacy records, `process(mirror_url)`.
- Image records: `is_image_record` (type image; album only if filename has `IMAGE_EXT`; files by extension; not origin extra). `match_attachments(entries, attachments, content)`: exact name → normalized name (`_norm`: alnum only, strip SPOILER_) → exclude `📎` missing names (`_missing`) → order when counts match.
- Link images: `is_link_image_record` (rec.img set, not an image record) → `_link_images` sends each URL directly to the App, joins texts into `ocr`.
- Failure handling in `run()`: a mirror whose `process` returns False goes to the BACK of the queue (so one stubborn mirror no longer blocks the others); `_fail_streak` counts consecutive failures and only when every queued mirror failed in a row does it wait `_backoff` (30 s doubling to 900 s) and show `暫時失敗，N 秒後重試：<reason>` (`_why`) in the task detail via `_detail()`. Timeouts (`App 沒有回傳`) count toward `MAX_TRIES` per record/URL (then treated as `（無法取得圖片）`); transient download errors (`retry`) count in `_dl_fails` up to `DL_TRIES=5`. `flush()` waits for pending background writes at most `FLUSH_SEC=180` (detail `等待寫入 Discord（N 筆）`) and then lets them finish in the background.
- `_ask(url, name)` → result dict or None (timeout `TIMEOUT=120`); pacing `PACE_SEC=0.4`; backoff for transient download errors (`retry`, `retry_after`); non-transient errors retried until `MAX_TRIES=3` (`_fails`) then `（無法取得圖片）`.
- OCR text of chat-like screenshots carries the `〔左〕`/`〔右〕` line prefixes (see OCRService `layoutText`; `clean` keeps them; `enrich.pipeline.ocr_caption` explains them to the image analysis, and falls back to the old 「沒有左右位置」 wording for older OCR text without markers).
- `needs_ocr`, `useful`, `clean` (Luhn card masking → `＊＊＊＊1234`). No failure log file. `scan_index()` (called at startup and by `cycle()` at every start of the `ane` lane, i.e. schedule or run-now) queues every record with no `ocr`, truncated legacy `ocr`, or `ocr == （無法取得圖片）` (image and link-image records) into `_retry_keys` and resets its `_fails`, so previously failed/unfinished images are redone each round; they end up `（無法取得圖片）` again only after `MAX_TRIES` failures or when no mirror attachment matches. `_clear_stale` clears ocr on album videos. Days with new text and an existing daily summary → `mark_days` (RECAP_DIRTY).

### ipc.py
Startup progress: `startup.stage(name, detail, done, total)` (module `startup.py`, `STEPS` = 連線 Discord / 檢查頻道與權限 / 連線外部 LLM / 載入記憶與提示詞 / 連線 Telegram / 載入 #關鍵字庫 / 載入 #總結與狀態 / 啟動各項工作) emits `{"type":"startup","stage","detail","step","steps","done","total"}` on stdout from the very beginning of `main.run` (the IPC server and its status lines only start later); `KwIndex.load` reports `on_progress(n)` every 500 messages and `Relayer.load_state` turns it into `已讀 N 則` (final `完成，共讀 N 筆紀錄` carries `total`). The App shows it while the core is `.starting` (the first `status` line ends it): `CoreController.startup/startupFraction/startupText` (progress bar: each step is an equal share, inside the #關鍵字庫 step `done ÷ last run's total`, remembered in UserDefaults `lastKeywordTotal`), `StateBadge` (dashboard) and `stateLabel` (`啟動中 6/8：載入 #關鍵字庫`).
Enabled when `INBOXRELAY_IPC=1`. JSON lines; logs go to stderr.
- App → core (stdin): `{"type":"lane","lane":"llm|ane|shot","action":"run_now|pause|resume"}`, `{"type":"hello","ocr":true}` (ane available), `{"type":"ocr_result","id":N,"text":"…","ms":N}` or with `"error","retry","retry_after"`, `{"type":"recap_reset"}`, `{"type":"llm_test","id":N,"server","model","key"}` (tests the form values via `llm.test_connection`; if it is the running server+model and succeeds, `ai_status` → connected and an offline `llm` lane is reset), `{"type":"stop"}`; legacy `recap_now|pause|resume`.
- core → App (stdout): `{"type":"startup",…}` while starting (see below), `{"type":"status", paused, tasks[{label,done,total,detail,line,progress}], backlog, probe_pending, summarized_until, recap_enabled, recap_phase, recap_time, next_recap_hours, ai_status, chat_title, rss_mb, running, lanes{llm|ane|shot:{label,mode,state,describe,queued,backlog,next_start}}}` every 2 s on change; `{"type":"ocr_request","id":N,"url":"…","name":"…"}`, `{"type":"llm_test_result","id":N,"ok":bool,"message":"…","ms":N}`.
`IpcServer(act, relayer, get_recapper, tg, cfg, ai_status, stop)`, `lanes_status()`, `emit()`.

### recap/runner.py / recap/daily.py
- `RecapChannel`: posts in #總結 with marker line `DAILY|YYYY-MM-DD`, `MONTHLY|YYYY-MM`, `YEARLY|YYYY` (optional ` i/n` for chunks, may be prefixed `-# `); `load`, `publish` (edit chunks in place, extra chunks are posted at the bottom, surplus ones deleted), `publish_post(kind, key, title, body, footer)` (what `summarize_day/month/year` call: a recap is split into as many messages as needed with `make_chunks` so no source link has to be dropped; but chunks beyond the existing count would land after later recaps, so when the key already exists and is not the last recap in the channel (`is_tail`) it keeps its existing message count via `make_chunks_fit(max_chunks=…)`, which turns the trailing source links into plain `↗n` numbers (then truncates) until it fits), `body(kind, key)` (all chunks joined, used by search), `remove`, `exists`, `has`, `first` (first chunk; monthly/yearly citations link to it), `keys`, `forget_message`. A full rebuild (`Recapper.reset_all`, the App's 「清空並重新總結」) posts every recap in timeline order at the bottom, so every recap can be multi-message there.
- `Recapper.cycle()`: `wait_ready()` — waits until `ready()` (the Enricher is `drained` AND every callable in `Recapper.waits` is true; main adds `OcrWorker.drained`, so recaps start only after image text recognition AND AI analysis incl. the re-analysis that OCR results trigger are finished; checked every `DRAIN_POLL_SEC`, and once more after `SETTLE_SEC` so the hand-off gap between OCR finishing and the re-analysis being queued cannot slip through) → `tick()` publishes in timeline order (`timeline_key`): days with data minus existing DAILY, dirty days (RECAP_DIRTY), month after its last day, year after December. Offline error → `lane.go_offline`. `summarize_day/month/year`, `reset_all`, `add_note` (RECAP_NOTES), `trigger_now`, `set_schedule`, `month_ready`, `year_ready`, `unenriched`. `TYPE_LABEL` maps record types.
- daily: `Item(time, type_label, text, mirror)`, `build_material`, `build_prompt`, `link_citations` (`[↗n](<url>)`), `split_message`.

### search/engine.py / search/channel.py
- engine: `parse_query` (LLM SEARCH_PARSE → date range + ≤20 terms) → the date range is discarded when the question has no time wording (`has_time_hint`) and `expand_terms` adds the common words of the category (e.g. food: 吃/餐/飯/麵/便當/火鍋…, from `CATEGORY_TERMS`, ≤45 terms) → `collect`/`rank` ALL candidates from KwIndex (excerpt, filename, kw, gist, ocr window) + DAILY summaries, ordered by relevance (`score_text`: each term hit counts once + 0.2 per repeat; the user's/model's own terms `Query.core` weigh 2, the category words added by `expand_terms` weigh 1; ties → older first; when more than a page, only records with a hit are kept; `select` is an alias) → `page(ranked, start)` takes one page of `MAX_CANDIDATES=150` (sorted by date for the model) → material capped at `MAX_MATERIAL=50,000` chars (`daily.build_material(per_item, max_chars)`) → SEARCH_ANSWER (asks to list every matching candidate, up to 30, when the user wants a list; `max_tokens` 2500, thinking mode 8000; an empty answer — e.g. thinking cut off — is retried once with thinking off, then a friendly message) → `format_reply` (dates, no message ids). `search_ranked` returns `SearchResult(reply, query, total, shown, rest)` (`rest` = lower-ranked candidates not shown yet); `answer_page(llm, question, batch, thinking)` is the model step shared by the first page and 「顯示更多」 (the material gives each candidate its full `YYYY-MM-DD HH:MM` and SEARCH_ANSWER requires full `年-月-日` dates in the bullets); `search()` is the old tuple-returning wrapper.
- 「顯示更多」: when `rest` is not empty the reply's last message carries a button (`channel.MoreButton`, a `discord.ui.DynamicItem` with `custom_id` `inboxrelay:search_more:<8 hex>`, registered with `client.add_dynamic_items`, so it survives restarts). The token maps (in memory, newest `MORE_KEEP=40` searches) to `MoreState(question, query, rest, offset, total, where, thinking)`; pressing it (`SearchChannel.on_more`) removes that button, runs the next `MAX_CANDIDATES` ranked candidates through `answer_page` and replies with them (`-# …關聯度第 151～300 名（共命中 N 則）`) plus a new button while candidates remain; nothing is processed if it is never pressed; after a restart the token is gone and the button answers that the search is no longer available. Footer wording: `命中 N 則，先看關聯度最高的 150 則`.
- channel: `SearchChannel` (#搜尋 live when SEARCH_LIVE; 搜尋測試 searches AI-test content); follow-ups via reply chain (`MAX_TURNS=3`, `TURN_CHARS=600`), SEARCH_FOLLOWUP routes general vs search.

### chat.py
`ChatChannel` for DC_CH_CHAT. Reply generation lives in `_respond(thread, m)` (called under the thread lock by `on_message` and `on_edit`; returns `(user_text, memos)` for `_remember` after the lock). Queueing: messages sent while a reply is being generated wait on the per-thread `asyncio.Lock` (FIFO). The history is the whole thread, so the first reply answers everything sent so far: `answer` records `_snap[thread]` (highest user message id it saw) and `_respond` commits it to `_seen` (and the reply ids to `_last_reply`) after posting; a queued message with `id <= _seen` is skipped (`已被上一次回覆一併回答`). A message that arrived after the snapshot but before the reply was posted (id below the reply's) is moved after that reply by `_reorder_late` so the model sees it as a new question; edit-regenerate resets `_seen/_last_reply`. Deleting a thread cancels its work: `_working(thread_id)` registers the current task in `_work` (around the locked part of `on_message`/`on_edit` and in `_retitle`); `on_thread_delete(thread_id, parent_id)` (registered with `dc.add_thread_delete_handler`, fed by the `on_raw_thread_delete` event) and the delete button (`cancel_thread_work` before `thread.delete()`) cancel those tasks, which also cancels the pending LLM HTTP request (`LLM.chat_messages` hands its queue slot back on cancel). `_retitle` posts a `-# 🏷️ 正在重新命名對話標題…` note first (ignored by `_collect`) and deletes it when finished or failed. Edit-to-regenerate: `on_edit(payload)` (registered with `dc.add_edit_handler`) reacts only to real content edits (`payload.data["edited_timestamp"]`, remembered per message in `_edited` so one edit triggers once; link-preview/embed updates carry no such field) of a non-bot message in a chat thread (or of the thread's starter message in the main channel, found by thread id = message id and refreshed in `_starters`); if it is the last user turn (`_collect`, not `_is_bot_turn`) it deletes every bot message after it (replies, usage footers, `-#` notes) and calls `_respond` again with the edited content; if it is not the last user message nothing happens, and if a `SUMMARY_HEAD` message follows it a `-#` note says it cannot regenerate after compaction. Root message → thread (`thread_title`); per-thread lock; history = thread messages (`merge_roles`, `trim_history` by token budget from LLM_CONTEXT − output − `SAFETY_TOKENS`, chars↔tokens ratio `_tpc` calibrated from usage); images (latest `MAX_IMAGES=4`, ≤`MAX_IMAGE_BYTES`), documents (`DOC_CHARS=20000`), links (`LINK_CHARS=6000` via fetch_text).
New-chat button: `main.run` creates the `ChatChannel` right after the channel checks and runs `register_views()` + `setup()` immediately (chat needs only Discord and the external LLM, not #關鍵字庫), so buttons and replies work while the rest of the core (index, recap, enrich…) is still loading; `setup()` → `_ensure_button()` posts/pins the button message. Views use `custom_id=NEW_CHAT_ID`; `_ensure_button()` posts/pins one `BUTTON_TEXT` message (found by `BUTTON_MARK`; an existing one with different text is edited in place); control messages carry only `-# 💬` (no hint text) in the chat channel; click → `open_thread()` creates a public thread named `placeholder_name(date)` (`YYYY-MM-DD_<color>-<fruit/animal>-<4 digits>` in English, e.g. `2026-10-04_black-dog-8818`; `is_placeholder` also accepts the earlier Chinese and `_xxxx` forms) and tracks it in `_auto`. Auto-title: after the first reply, if `_wants_title` (thread in `_auto` — also threads opened from a root message — or name still a placeholder; manually renamed threads are left alone) → background `_retitle()` asks `llm.chat(TITLE_PROMPT, use_memory=False)`, `clean_title` (≤30 chars), renames to `<title>` only (no date); failure keeps the old name and retries on the next reply.
Button responses: handlers `defer()` the interaction (no reply/ephemeral row); errors are the only ephemeral replies; `_LoggedView.on_error` logs handler exceptions. Thread controls: after 新對話 / 新增聊天 / 從這裡分支 the bot posts a plain message in the main chat channel via `_announce(parent, "✅ 已開新對話\n<#id>" | "🔀 已分支（n 則）\n<#id>", controls=True)` carrying persistent `ThreadControlView(chat, url)` (leftmost 💬 開啟聊天 link button to the thread's `jump_url` via `_thread_url`, then ➕ `open_thread`, 🔀 `branch_thread`, 🗑️ `ask_delete`; the url-less instance is what `register_views` registers); the target thread is resolved by `_target_thread` from the `<#id>` mention in that message (or the thread itself for old in-thread control messages). Nothing is posted inside new threads. Branch: new thread `🔀 <name>`; under the source thread lock `_collect(src)` (shared with `history`) is replayed by `_copy_message`: bot messages (replies, `SUMMARY_HEAD` summaries) verbatim; user messages re-posted by the bot as `USER_HEAD` (`👤 **name**` + body, every chunk carries the head, attachments re-uploaded in memory within `guild.filesize_limit`, ≤10 files) and recognized via `is_copied_user`/`_is_bot_turn` so the LLM still sees them as user turns; because the summary message is copied too, `history()` of the new thread starts from the last summary (users see everything, LLM reads only the compacted part). Delete: `ask_delete` posts a confirm message next to the controls with non-persistent `ConfirmDeleteView` (120 s, requester only; cancel/timeout delete just that message); confirm → `delete_thread` deletes the thread, the confirm message, the control message and the parent's `thread_created` system message. Deleting threads that contain messages needs the Manage Threads permission (「管理討論串」) even for threads the bot created (Discord returns 403/50013 otherwise); `setup()` logs `PERM_HINT` when it is missing. Renaming its own threads needs no extra permission.
Commands: `THINK_PREFIX="/思考"`, `COMPACT_CMD="/compact"`. Footer `USAGE_TAG="-# 🧮"` (上下文 used/ctx tokens). Auto-compact at `chat_compact_pct`: `compact()` streams CHAT_COMPACT with text progress bar (`progress_bar`, ▰▱, edited every 2 s, deleted after) → summary messages starting `SUMMARY_HEAD="🗜️ **對話摘要"`; `history()` resumes after the last summary.
Prompt/memory in DC_CH_CHAT_PROMPT (latest `#提示詞` / `#記憶`, default published if missing, `PROMPT_LIMIT=1900`); memory = `MemoryStore(apply_to_llm=False, upgrade_prompt="CHAT_MEMORY_UPGRADE")`, refresh flag `chat-memory-rules-v2`. Model marks memorable lines with `MEMO_TAG="〔記憶〕"` (`split_memos`, stripped from replies); `EXPLICIT_RE` (記住|幫我記|記得我) → correction + immediate upgrade; `SOON_RE` (以後|之後|下次|從現在開始|不要再|別再|一律) + memo → immediate; else after `IDLE_UPGRADE_SEC=600` idle. Uses `llm.chat_messages` (interactive priority).

### feedback.py
`FeedbackChannel` (DC_CH_NOTIFY, owner/editors only): FEEDBACK_ROUTE JSON → `fix_daily` (RECAP_NOTES + rewrite day + month), `fix_monthly`, `memory` (`memory_note` imperative rule → `add_correction` + `upgrade`), `other`. `parse_route`.

### aitest.py
`AiTestChannel` (DC_CH_AI_TEST): analyze any message like the pipeline; `!總結 [date|全部]` preview; `!說明`. Summarizes and screenshots every URL in the message (no cap).

### importer/
- reader.py: streaming `result.json` (ijson) → `HistMsg(tg_msg, dt, text, type, rel_path, file_name, missing_reason)`; photo file name = basename of export path; missing → `file_<id>`; `to_bot_chat_id`.
- packer.py: `Packer`/`pack_all` → `Packet`s (same-day text merged ≤2000; attachments one per message; date header first of day).
- job.py: `ImportJob` (plan, dry-run, checkpoints, IMPORT lock, `MIRROR_INTERVAL`), records origin=`import`.
- extra.py / extra_job.py: `import-extra` → `DayDoc(day, kind, icon, name, body, …)` from 財政部 invoice CSV (`is_invoice_csv`, `invoice_docs`) or JSONL (`parse_jsonl`: day, kind, icon, name, body, title?, excerpt?); one post per day+kind in DC_CH_EXTRA; KW record origin=`extra`, tg_chat=0 (`EXTRA_CHAT`), type `invoice`/kind; changed days → RECAP_DIRTY. Personal-format converters must not be added to the repo.

### reset.py
`run_reset(...)`: deletes mirror messages, bot posts in keywords/recap/screenshot/extra, `STATE_TAGS=("STATE","IMPORT","LIVE","RECAP_DIRTY","RECAP_NOTES")`; flags `--memory --chats --prompts --all`. Telegram untouched. (App `recap_reset` IPC is different: it calls `Recapper.reset_all()` — clears #總結 and re-summarizes.) CLI `main.py reset-recap [--yes]` (needs the App/core to be closed because of the instance lock) does the same as `reset_all` without the App: purges the bot's messages in #總結, empties `summarized_until/monthly_until/partial_days` in STATE and clears RECAP_DIRTY; other data and RECAP_NOTES are kept.

### memguard.py / statusbar.py
- memguard: RSS every minute; > soft → clear caches; 3× > hard → exit (App restarts) or warn.
- statusbar: terminal bottom bar via DECSTBM; disabled when not a TTY or `STATUS_BAR=false`.

### tests/
unittest, run: `python -m unittest discover -s relay-core/tests`. Files: test_activity, test_chat, test_enrich, test_extra, test_formatting, test_importer, test_lanes (incl. offline), test_link_image, test_m4, test_ocr, test_ocr_message, test_recap, test_records_album, test_reset, test_reverse, test_search, test_shot.

## 4. App (app/*.swift, macOS 14+, built with swiftc, no Xcode project)

| File | Role |
|---|---|
| InboxRelayApp.swift | `@main InboxRelayApp`: MenuBarExtra + main window; `MenuIcon` (the menu bar label, created at launch) opens the main window once at launch when the per-machine preference `AppPrefs.showMainWindowKey` (`showMainWindowOnLaunch`, UserDefaults, default true, not synced with the settings folder) is on, and activates the app; launch at login via `SMAppService.mainApp` |
| CoreController.swift | Launches bundled core (`Contents/Resources/core/run.sh`) with env from settings + `INBOXRELAY_IPC=1`; restart with backoff; parses status JSON (`CoreStatus`, `CoreTask`, `LaneStatus{label,mode,state,describe,queued,backlog,next_start}`); lane actions (`llm|ane|shot`; `LaneRow` shows the `shot` row only when the core reports that lane); launch arg `--instant` → once status shows `llm` and a connected `ane` lane, sends `run_now` to `llm`, `ane` and `shot` (if present); `handleOCR` (calls OCRService, replies `ocr_result` with retry info); `recapReset`; `testLLM()` (sends `llm_test` with current form values, `llmTesting`/`llmTestResult`, 25 s timeout); in-memory `logs` |
| OCRService.swift | (`layoutText`: when the recognized lines look like a chat screenshot — at least 3 lines whose box centre x < 0.42 AND at least 3 with x > 0.58 — the text is sorted top-to-bottom and each line is prefixed `〔左〕`/`〔右〕` (centre x < 0.42 / > 0.58, the middle stays unmarked: right = the user, left = the other party); other images keep Vision's order without markers. Only newly recognised images get this; existing OCR text is not redone) (the image is decoded with `kCGImageSourceShouldCacheImmediately: true`: Telegram photos are progressive JPEGs and a lazily decoded CGImage makes Vision always throw CRImageReaderError) `recognize(urlString:completion:)`: ephemeral URLSession (no cache/cookies), HTTP status check (non-2xx → retry with Retry-After), `VNRecognizeTextRequest` (accurate, zh-Hant + en-US, language correction), Neural Engine first, then CPU/GPU, then a re-render to standard sRGB 8-bit RGBA (long side ≤ 4096; works around `CRImageReaderError` on unusual formats); serial queue, image in memory only |
| SettingsStore.swift | `SettingCatalog.fields` (incl. `SHOT_SCHEDULE/START/END`; time fields visible per mode; key = env name, label, kind text/secret/toggle/time/choice/number, section 連線/頻道/外部 LLM 設定/排程/功能/…); secrets in Keychain; others in UserDefaults or synced folder `settings.json` (optional secrets there); first run imports `.env` |
| Views.swift | Menu content (lane cards: colors running green/teal, waiting blue, offline red "LLM 離線", paused orange; run/pause buttons), dashboard, settings (「一般」 section: 「App 開啟時顯示主視窗」 toggle (`@AppStorage`) and 「登入時自動啟動」; 「測試連線」 button under 外部 LLM 設定 with ✅/❌ result; reset button with confirmation, Cancel default), log view |
| core_run.sh | Bundled launcher: venv in `data_dir/venv` (requirements hash stamp), `INBOXRELAY_ENV=data_dir/.env`, stdout reserved for JSON |

tools/linkshot.swift (response policy: text/*, json, javascript/ecmascript, yaml, toml, sh, sql, ndjson, image/*, html/xml and PDF are displayed — textual types even when WebKit's `canShowMIMEType` is false; other types or > 30 MB are cancelled with exit 4 `不是可截圖的網頁`): WKWebView with `WKWebsiteDataStore.nonPersistent()`, loads URL, settles, full-page snapshot → JPEG on stdout; `--insecure` bypass prints `CERT_BYPASSED`. tools/make_icon.swift: generates app icon at build.

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
| #投遞箱鏡像 | mirror posts (date header, text, files, 📎 lines); AI reply under mirror (`**🔗 url**`, `**🖼️ name/url**`, `**📄 name**` + `> ` lines, `📸 截圖：[↗1](<url>)`); progress message (always the last message) | relay.py, enrich/pipeline.py, activity.py |
| #關鍵字庫 | `KW|v2` records, `OCR|chat|msg` texts, activity message | records.py, kwindex.py, activity.py |
| #資料庫 | STATE, IMPORT, LIVE, RECAP_DIRTY, RECAP_NOTES, `#記憶 vN｜…`, `#提示詞 NAME vN｜…` | state.py, memory.py |
| #總結 | `DAILY|…` / `MONTHLY|…` / `YEARLY|…` posts | recap/runner.py |
| 連結預覽與進度 | `📸 when｜url` + image `preview.jpg` | enrich/screenshot.py, activity.py |
| AI 對話 | threads; footer `-# 🧮`; `🗜️ **對話摘要` | chat.py |
| AI 對話提示詞與記憶 | `#提示詞`, `#記憶` | chat.py |

## 7. Build & run

- Dev: `./run.sh [subcommand]` (uses repo `.env`, `.venv`).
- App: `./install_app.sh` (build → replace /Applications/InboxRelay.app → open). Build log `app/.build.log`.
- Tests: `python -m unittest discover -s relay-core/tests`.
- Python deps: `relay-core/requirements.txt` (python-telegram-bot, discord.py, ijson, openai, trafilatura, lxml_html_clean, python-docx, openpyxl, python-pptx, defusedxml, charset-normalizer, pypdf, pypdfium2, psutil).
