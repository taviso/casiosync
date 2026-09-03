# AGENTS.md — Casio G-Shock Project

## Overview

This project supports syncing Casio ABL-100WE watches from Linux.

- **`gshock_api/`** — A Python library (`gshock-api` on PyPI) for communicating with Casio watches via BLE using Bleak.
- **`casiosync.py`** — Root-level standalone script that syncs lifelog/step data from a watch and optionally emits structured log output.
- **`stepcount.diff`** — A historical patch file that was already applied to the codebase.

The library is authored by Ivo Zivkov (`izivkov@gmail.com`) and published on PyPI as `gshock-api`.

stepcount.diff---

## Essential Commands

Everything uses **[uv](https://docs.astral.sh/uv/)** as the package manager. Commands run from `gshock_api/`:

```bash
# Install dependencies
uv sync

# Run the test suite
uv run poe test

# Format + lint + type-check
uv run poe format

# Lint + type-check only (no fixes)
uv run poe check

# Lint only with auto-fix
uv run poe lint

# Full quality check: format, lint, type-check, dead code detection, complexity
uv run poe quality

# Run the integration/API test suite (requires a real watch)
uv run src/examples/api_tests.py

# Build distribution packages
uv build

# Upload to PyPI
uv run twine upload dist/*
```

**Important:** Run the `gshock_api` library commands from the `gshock_api/` directory. The root-level `casiosync.py` manually injects `gshock_api/src` into `sys.path` — it is **not** part of the library package. (The `lifelog.py` corpus tests under *Testing* run from the repo root instead.)

---

## Architecture

### Library Structure (`gshock_api/src/gshock_api/`)

```
gshock_api/              # Public API facade
connection.py            # BLE connection management (Bleak)
scanner.py               # BLE device discovery
message_dispatcher.py    # Routes incoming BLE data + outgoing JSON commands
watch_info.py            # Per-model capability lookup
casio_constants.py       # BLE UUIDs + characteristic code mappings
cancelable_result.py     # Async awaitable primitive
utils.py                 # Hex ↔ bytes conversion utilities
iolib/                   # Feature-specific I/O handlers
  connection_protocol.py  # Protocol interface (request/write)
  actions.py              # BLE action dataclasses (Write, Read)
  packet.py               # Packet protocol types (Header, Payload, Trailer)
  *_io.py                 # One module per watch feature
```

### Core Patterns

**1. Dual-class I/O pattern (Functional + Stateful):**

Every I/O module has two layers:

- `*IOFunctional` — Pure functions for encode/decode/command generation. Contains no mutable state, side effects, or I/O. These are the "Monoid" pattern referenced in the README.
- `*IO` — Stateful wrapper with static class-level state (`connection`, `result`). Acts as the interpreter that calls the functional layer and handles async BLE I/O.

Example: `WatchNameIOFunctional.decode()` is a pure function; `WatchNameIO.request()` creates a `CancelableResult`, sends the request, then awaits the response.

**2. Request-Response flow:**

1. Caller invokes `SomeIO.request(connection)`
2. `.request()` creates a `CancelableResult` and sends a write to handle `0x0C` (READ_ALL_FEATURES) or via `connection.request()`
3. The watch responds with a BLE notification
4. `Connection.notification_handler()` routes the notification to `MessageDispatcher.on_received(data)` or `on_drsp_received()` / `on_convoy_received()`
5. The dispatcher calls `SomeIO.on_received(data)`, which decodes and calls `result.set_result(value)`
6. The original caller's `await result.get_result()` unblocks

**3. MessageDispatcher routing:**

Two separate maps:

- `watch_senders` — Maps JSON action strings (e.g. `"SET_ALARMS"`) to `send_to_watch` async handlers. Used for JSON-based commands sent through `Connection.send_message()`.
- `data_received_messages` — Maps characteristic code (the first byte of a BLE notification) to synchronous `on_received` handlers. Used for raw BLE characteristic data.

**4. GATT handle model:**

The `Connection` class maintains a `handles_map` from integer handles (e.g. `0x0C`, `0x0E`, `0x11`) to BLE characteristic UUIDs. Writes use handles, not UUIDs. The `characteristics_map` is populated at connect time from the actual GATT services discovered on the device.

Key handles:
- `0x0C` — READ_ALL_FEATURES (write-without-response, sends commands)
- `0x0D` — NOTIFICATION (write-without-response, sends notifications)
- `0x0E` — ALL_FEATURES (write-with-response, writes data back)
- `0x11` — DATA_REQUEST_SP (used for lifelog/step data transfer)
- `0x14` — CONVOY (write-without-response, convoy notifications)
- `0x17` — SP_REQUEST (GW-BX5600 read-modify-write protocol)
- `0x19` — SP_DATA (GW-BX5600 write-with-response + notify)

**5. Watch model detection:**

On BLE discovery, `scanner.py` passes the device name to `watch_info.set_name_and_model()`. The `WatchInfo` dataclass matches the name prefix to a `WatchModel` enum, then uses a `ChainMap` to layer model-specific capabilities (world city count, alarm count, DST count, etc.) over defaults. Feature flags like `hasNewTimeFormat` and `hasSecondDial` control which protocol paths are used.

### Root-level scripts

- **`casiosync.py`** — Standalone BLE sync script. Connects to a watch in any mode (pair, time-sync, auto-sync), fetches lifelog step data, then syncs time. Supports `--addr`, `--timeout`, `--peek`, `--log`, `--quiet` flags. Adds `gshock_api/src` to `sys.path` directly. Outputs compressed base64 lifelog buffers with `--log`.
- **`lifelog.py`** — Core 400-byte lifelog buffer parser. Contains `Lifelog.parse()` (entry point), `Lifelog.lifelog_entries()` (structured entry list via `Entry` dataclass), detailed human-readable report mode with integrity checks, previous-day recovery, and distance component tracking. Also works standalone: `python lifelog.py logs/*.txt`.
- **`parse_lifelog.py`** — Thin CLI wrapper (~75 lines) around `lifelog.py`. Imports `Lifelog` and calls `lifelog_entries()` to generate sorted lifelog entries in a parseable format.
- **`data/`** — Reference material from a decompiled Android APK bundle. Not buildable or runnable.

### `lifelog.py` parser internals

`Lifelog.parse()` is the single entry point (raw 400 bytes → `Lifelog` dataclass). Its pipeline:

1. **Decode the header/tail** — timestamp @0, live totals @374/@378, pending walk @382, BCD total @396.
2. **Locate the front-record boundary** — `find_record_end()` returns `record_end` (exposed as `Lifelog.auxiliary_offset`), the byte where today's committed front records end. Every post-boundary region is addressed *relative* to it. Hour < 18 uses the fast path `record_end = 6 + 10*max(0, hour-6)`; otherwise `_score_candidate()` scores candidates in `range(6, 150, 10)` against the reconstruction target.
3. **Read today's activity** — front records at `range(6, record_end, 10)` as `Activity` (5 intensity buckets, newest first).
4. **Read the auxiliary arrays** — `auxiliary_a`/`auxiliary_b` at `record_end` and `record_end+50` (the M1/M2 regions). In normal-day layout these are today's populated history; in rollover they are *not* today's data.
5. **Classify state** — `wiped` (non-peek clear: front + pending + M1/M2 all zero but @374 > 0) and `transitional` (hour 18 dual layout, see gotcha 18).
6. **Decode the fixed regions** — `distance_prefix()` (today's distance stack @246), `read_daily_summaries()` (7-slot ring @318).
7. **Recover yesterday** — `parse_previous_day()` (rollover only): 13 front records (hours 23:00→11:00) at `off..off+130` + M1/M2 at `off+130`/`off+180`, each truncated against the distance-stack barrier @246 (`safe_first`/`safe_second`, and the front-record bound check).
8. **Reconcile** — compare reconstructed components against @374/@378 and emit `warnings`.

**Unknown-field invariant:** `known_fields()` is the *sole* registry of decoded byte ranges; `unknown_fields()` returns its complement and drives the `Unknown fields` report. Any newly-decoded region must be added to `known_fields()` so it stops appearing as unknown — do not special-case unknown ranges elsewhere.

---

## Code Style & Conventions

- **Python 3.12+ only** (uses `type` statements, PEP 695 syntax)
- Double quotes for strings (enforced by ruff)
- 4-space indentation, 88-char line length (Black-compatible)
- Google-style docstrings (enforced by ruff `pydocstyle`)
- Full type annotations required everywhere except test files (enforced by ruff `ANN` + basedpyright strict mode)
- Ruff rules: E, F, I, UP, B, C4, T20, SIM, N, Q, RUF, ASYNC, S, PTH, ERA, PL, PERF, ANN, ARG, RET, TCH
- No `print()` statements in library code (T20 rule)
- Security linting enabled (S rules from flake8-bandit)
- Test files are exempt from `ANN` (type annotations) and `S101` (assert) rules
- `__init__.py` is exempt from `F401` (unused imports)
- Ruff `isort` configured with `known-first-party = ["gshock_api"]`

---

## Testing

- Framework: **pytest** with **pytest-cov** for coverage
- Tests live in `gshock_api/tests/test_code.py` (unit tests for functional IO layers)
- The `gshock_api/src/examples/api_tests.py` script is an integration test that **requires a real watch in BLE range**
- Run unit tests: `uv run poe test` (from `gshock_api/`)
- Run integration tests: `uv run src/examples/api_tests.py`
- CI: GitHub Actions Conda workflow (`gshock_api/.github/workflows/python-package-conda.yml`), runs on push. Stale: it pins Python 3.10, but the project now requires Python 3.12+ (`requires-python = ">=3.12"`).

### Offline test corpus (`logs/`)

`logs/` holds offline dumps of the raw 400-byte buffer captured from an ABL-100. These are the primary regression harness for `lifelog.py` — no watch required. Each file is a 400-byte hex record (optionally with leading `#` note lines) or a compressed base64 payload (see gotcha 16).

- **Naming** — two conventions from two capture periods:
  - `YYMMDDHHMMSS.txt` (e.g. `260801064401.txt`) — early manual-capture session, 2026-07-31 → 2026-08-02.
  - `YYYY-MM-DD-HHMM.txt` (e.g. `2026-08-14-1830.txt`) — auto-sync run, 2026-08-02 → 2026-08-27.
- **`#` note lines** — human annotations stripped by `read_input()` and surfaced as `Notes:` in the report. They record what the wearer was doing (e.g. "walked to the park") and, for the 8 known MISMATCH/wiped dumps, *why* the mismatch is expected (non-peek clear). Treat these as authoritative ground truth, not parser output.
- **Special files** — `trunca.txt`/`truncb.txt`/`truncc.txt` (boundary/truncation edge cases), `vacation.txt` (10-day gap with hand-recorded end-of-day totals), and `2026-08-28-1230.txt`/`2026-08-29-0030.txt` (the 12:30→00:30 pair that anchors the rollover hour mapping).
- **Regression command:**
  ```bash
  for f in logs/*.txt; do python lifelog.py "$f" >/dev/null 2>&1 || echo "FAIL: $f"; done
  ```
  Every parser change should pass all files. The invariant to preserve: no file raises, and the 8 annotated MISMATCH/wiped files still report the same status with the same reasoning.
- **Golden snapshot test:** `tests/golden_test.py` regenerates the canonical `lifelog ...` entry lines for every dump and diffs them against `tests/golden.txt`. Run `python tests/golden_test.py` to verify, `--update` to accept changes after a deliberate parser change. This is the authoritative guard for the structured entry output; the `#` note lines and human-readable report are not snapshotted.

---

## Gotchas & Non-Obvious Details

1. **`uv` is required.** Standard `pip` workflows won't work — the project uses `uv.lock` and `uv`-specific tooling.

2. **Two time-setting protocols.** Most watches use the standard ALL_FEATURES path (`TimeIO`). The GW-BX5600 and GMW-BZ5000 use a completely different SP read-modify-write protocol (`GwBx5600TimeIO`) that fragments notifications across BLE MTU boundaries and reassembles them. This is controlled by `watch_info.hasNewTimeFormat`.

3. **Connection modes.** The watch has three button-triggered BLE modes plus a capability-based variant:
   - **LOWER_LEFT (C) — Pair / full-features.** All characteristics present. Full sync: time → condition → lifelog.
   - **LOWER_RIGHT (B) — Time sync.** All characteristics present (including ALL_FEATURES at 0x0E), but `set_time` → `initialize_for_setting_time()` kills DATA_REQUEST_SP (0x11) writability. Lifelog must be fetched **before** `set_time`.
   - **NO_BUTTON (auto-sync at midnight/periodic).** Watch initiates connection to upload lifelog. Same constraint: lifelog before time.
   - **True time-sync mode.** When `CASIO_ALL_FEATURES_CHARACTERISTIC_UUID` is absent entirely. `Connection.is_time_sync_mode` detects this. Only time sync is possible; button characteristic (0x10) is not exposed.
   `casiosync.py` handles all modes with a single lifelog→time flow.

4. **`CancelableResult` pattern.** Every IO handler uses a static `CancelableResult` to bridge async BLE notifications with request callers. If `result` is `None` when `on_received` fires, a `RuntimeError` is raised. The timeout is typically 10 seconds.

5. **Notification handler is self-adapting.** `Connection.connect()` dynamically subscribes to **every** characteristic that supports notify/indicate — no per-model whitelists. This makes the library model-agnostic at connection time.

6. **Hex string encoding.** The Casio protocol uses space-separated hex strings with `0x` prefixes internally (e.g. `"0xA3 0x01 0x0C"`). The `utils.py` module converts between bytes, compact hex strings, and space-separated hex strings. `to_casio_cmd()` converts a compact hex string to raw bytes for writing.

7. **Watch name used for model detection.** The BLE device name (e.g. `"CASIO GW-B5600"`) is parsed in `WatchInfo.set_name_and_model()` to determine capabilities. The model prefix determines alarm count, world city count, DST behavior, and time format.

8. **The `data/` directory is reference-only.** It contains a decompiled Android APK (`decompiled/`) with Java sources, plus the APK bundle assets (Flutter assets, images, res XML, BLE snoop logs). Do not try to build or run any code in `data/`.

9. **App notifications only work on specific models.** The `AppNotificationIO` supports DW-H5600, GBD-H2000, GBD-200, GBD-100, GBD-800, GBD-100BAR, GBX-100. Other models silently skip notification features.

10. **Lifelog protocol.** Step data transfer uses a two-characteristic protocol: DATA_REQUEST_SP (`0x11`) for control (start/end commands) and CONVOY (`0x14`) for payload. The `LifelogIO` handler reassembles the full 400-byte lifelog buffer. The core parsing module is `lifelog.py` — it provides `Lifelog.parse()` (raw bytes → structured data), `Lifelog.lifelog_entries()` (structured data → sorted `Entry` list for log output), and a detailed human-readable report mode. `parse_lifelog.py` and `casiosync.py` are thin consumers that call these APIs.

11. **Static class-level state.** IO handler classes use `ClassVar`-style static attributes for `connection` and `result`. This means they are **not re-entrant** — only one request of a given type can be in-flight at a time. This is by design since there's only one watch connection.

12. **ABL-100 has special behavior.** The ABL-100WE model has a step counter but doesn't support reminders, app notifications, or battery level sensing (`hasBatteryLevel: False`). The `stepcount.diff` patch added lifelog support and skipped notifications for ABL-100.

13. **Peek vs non-peek.** `--peek` skips sending the end command (`0x04`) to the watch, preserving the hourly buffer for the next sync. Without `--peek`, the end command clears the hourly breakdown (front records + M1/M2) but the daily counter at @374 persists. The watch may display "ERR" in peek mode (expects acknowledgment) and "OK" in non-peek mode.

14. **Lifelog entry format.** `Lifelog.lifelog_entries()` returns `list[Entry]` where each `Entry` has `.timestamp`, `.steps`, `.intensity` (all 5 buckets, zeros included), and two flags: `.pending` (uncommitted current-hour walk) and `.summary` (daily total). Consumers format these however they like — no parsing logic lives in the consumers.

15. **Daily-summary ring and summary emission.** The buffer stores up to **7 days** of daily totals in a fixed ring at @318–@373 (7 × 8-byte slots: u32 steps + u32 distance). Older days fall out permanently. `lifelog_entries()` emits a `summary` entry (timestamped 23:59:59) for each ring slot **only when that day has no fine-grained data**. Yesterday is special: when `previous_day` recovery exists (rollover state, hour < 18), yesterday's summary is **skipped** because the recovery already covers it — either completely (sum equals the total) or partially (the missing portion was already synced and acknowledged by a prior non-peek sync). The summary exists purely as a fallback for days whose hourly detail is gone.

16. **Buffer serialization.** `casiosync.py --log` emits the raw buffer as `lifelog buffer="<base64(zlib.compress(400 bytes))>"`. `lifelog.py:read_input()` accepts both plain hex and this compressed base64 form, trying hex first and falling back to base64+zlib decompression.

17. **Single-buffer ambiguity after a non-peek clear.** The live counter at @374/@378 persists across a non-peek sync (the end command clears only the hourly breakdown, not the daily total). So a single buffer **cannot distinguish** "cleared, then accumulated new activity" from "missing/corrupt components" — in both cases the hourly components sum to less than @374. Example: `2026-08-02-1300.txt` reconstructs 6,235 components against a 9,812 total; the 3,577 difference is the pre-clear baseline, not corruption. We accept this tradeoff because resolving it would require cross-sync state (knowing what a prior sync already acknowledged), which a standalone parser does not have. The `wiped` flag only covers the *fully*-cleared case (no new activity); the "cleared + new activity" case still surfaces as a step/distance MISMATCH. Do not flag this as a bug — it is a documented limitation.

    **The 18:30 auto-sync is the most common trigger.** The watch auto-syncs at 00:30/06:30/12:30/18:30, and 18:30 is when the day history (M1/M2) fills. If a prior non-peek sync (e.g. 12:30) cleared the morning's steps, then the 18:30 dump's M1/M2 holds only the *post-clear* activity while @374 still counts everything — producing a MISMATCH whose size roughly matches the prior sync's total. `logs/2026-08-14-1830.txt` is a clean example: its 2,073-step gap exactly equals the total of the preceding 12:30 sync in `logs/2026-08-14-1230.txt`. Note this is a *distinct* manifestation from the transitional-boundary case in gotcha 18: at 18:30 the front records are already empty and the day history has filled, so the "missing" steps live nowhere in the buffer — they were cleared.

18. **Transitional-hour boundary ambiguity.** Hour 18 is the only hour with *two* distinct buffer layouts: before the day history fills at ~18:30 the front records still span the whole day (06:00–17:00, ~12 records, boundary 126), but after ~18:30 the front records reset to evening-only and M1/M2 carries the day history (small boundary). A single buffer cannot tell these apart without the timestamp's *minute*, because both score `_front_sum + pending == total` (the "with/without history" aliasing is symmetric). The parser handles this by treating hour 18 as transitional: it prefers the boundary where the front records alone equal the target. This resolves the catastrophic aliasing (it used to pick offset 16 and misparse the whole day), but it means the boundary may land at 116 rather than the "ideal" 126 when the 06:00 record is all zeros — a cosmetic distinction, since a zero 06:00 record produces identical output either way. The remaining edge case (hour 18:30–18:59, where the ideal boundary is small) is knowingly left unresolved; it is a 30-minute window and shares the same fundamental ambiguity. Do not "fix" the 116-vs-126 tie with a hardcoded `expected = 126` fast path — that reintroduces the aliasing failure for the 18:30+ sub-state.

---

## Appendix: Lifelog Buffer Format (400-byte Record)

*Reverse-engineered from dumps spanning 2026-07-31 22:38 → 2026-08-29 00:30 (ABL-100 / QW-5554 family).*

### Overview

The watch sends a 400-byte lifelog buffer via DATA_REQUEST_SP (0x11) / CONVOY (0x14). It holds up to two days of step data: **today** (live, growing through the day) and **yesterday** (preserved after midnight rollover). Day structure differs between the normal-day and rollover states.

### Header (0–5)

| Offset | Size | Content |
|---|---|---|
| 0–5 | 6 | BCD timestamp: `YY MM DD HH mm SS` |

### Key Field Reference

| Offset | Size | Type | Content |
|---|---|---|---|
| 0 | 6 | BCD | Timestamp |
| 6 | var | front records | Hourly committed activity, newest first (see below) |
| *arr_off* | 48 | u16[24] | **M1** — walking steps, 30-min blocks (06:00–18:00 window) |
| *arr_off*+50 | 48 | u16[24] | **M2** — running steps, 30-min blocks (06:00–18:00 window) |
| 246 | var | u16[] | **Today's committed distance stack** (meters, newest first). Fixed absolute position; grows per committed hour. The following region (@256–313) holds yesterday's distance stack in rollover state |
| 314 | 4 | u32 | Zero |
| 318–373 | 56 | 7×(u32,u32) | **Daily-summary ring** — 7 slots, newest first: slot 0 = yesterday, slot 6 = 6 days ago. Each slot is steps (u32) + distance (u32). Empty slots read `0xFFFFFFFE`. |
| 374 | 4 | u32 LE | **Total steps** — authoritative live counter (midnight→now) |
| 378 | 4 | u32 LE | **Total distance** — meters (midnight→now). Consistent stride ~0.38m/step |
| 382 | 6 | u16[3] | **Pending walk** — 3 intensity buckets for the in-progress walk. Cleared when the hour commits |
| 388 | 4 | — | Zeros |
| 392 | 4 | u32 LE | **Pending distance** — meters of the in-progress walk. Zero when idle |
| 396 | 4 | BCD LE | **Total steps in packed BCD** — second copy, e.g. 1485→`85 14 00 00`, 10070→`70 00 01 00` |

### Two-Day Rollover

After midnight the buffer carries previous-day detail alongside today's growing records. Detection currently uses a timestamp hour `< 18`.

The previous-day section is addressed relative to `off = 6 + 10*max(0, hour-6)`:

- **Front records** — 13 records, newest first, hours 23:00→11:00, at `off..off+130` (`off` = 23:00, `off+120` = 11:00).
- **M1** (walking, 24 × u16) at `off+130`.
- **2 bytes padding** at `off+178..off+180`.
- **M2** (running, 24 × u16) at `off+180`.

The 13 front records form one contiguous block, not two. The timestamp anchor is `2026-08-28-1230.txt`/`2026-08-29-0030.txt`: the 77 steps pending at 12:30 in `2026-08-28-1230.txt` (`(8, 69, 0)`) commit into `off+110` in `2026-08-29-0030.txt`, so `off+110` is 12:00, not 18:00. The "two 6-record blocks with M1 at `off+120`" reading is wrong: it strands five M1 values (e.g. `1473, 1080` in `260801064401.txt`) inside a supposed 12-byte padding gap. M1 really begins at `off+130`.

As today's front records grow, `off` increases and the previous-day section shifts right 10 bytes per committed hour. The distance stack at @246 independently overwrites the tail (M2 first, then M1, then the front records). Recovery truncates against @246 via `safe_first`/`safe_second` and the front-record bound check. `parse_previous_day()` reads the 13 records directly from the raw buffer (not via `auxiliary_a`/`auxiliary_b`) and registers them in `known_fields()` as "yesterday front intensity".

### Front Records (10-byte, newest first)

Each front record is **10 bytes (5 × u16)** representing the watch's **intensity classification** of steps within one committed hour. The accelerometer classifies each step into an intensity bucket; buckets are numbered 0 (lowest) to 4 (highest). For the ABL-100 bucket 4 (highest) is always zero; bucket 3 is a fourth, sparsely-used intensity bucket — nonzero in ~3% of front records (e.g. 16:00 and 17:00 in `2026-08-29-0030.txt`).

- Record `r` (0-based, r=0 is newest) belongs to hour `fetch_hour - 1 - r`.
- The sum of all five values equals the total steps for that hour.
- A walk spanning an hour boundary splits across two records: the pre-boundary portion commits into one hour's record and the post-boundary portion appears in the next hour's record (or in the pending-walk tail @382).

Example intensity breakdowns:

| Walk | Record | Interpretation |
|---|---|---|
| 07:00–07:15, 1485 steps | `[221, 1164, 100, 0, 0]` | Light:221, Moderate:1164, Vigorous:100 |
| 08:34–09:00, 2140 steps | `[314, 1786, 40, 0, 0]` | Light:314, Moderate:1786, Vigorous:40 |
| 13:00–13:35, ~3112 steps | `[421, 2750, 20, 0, 0]` | Light:421, Moderate:2750, Vigorous:20 (brisk walk to post office) |

The moderate bucket (index 1) always dominates; the vigorous bucket (index 2) is always tiny. The M1/M2 30-minute arrays are a coarser 2-category aggregation (walking vs running), while front records preserve the full 4-category decomposition (bucket 4 is always zero, bucket 3 is sparse).

### Day History (M1 / M2)

Two paired 24 × u16 arrays: **M1 = walking** steps, **M2 = running** steps, 30-minute resolution over a fixed **06:00–18:00** window. These are a coarser aggregation than the front-record intensity buckets.

- Slot `i` of the array maps to clock slot `(i + 10) mod 24`: position 0 = 11:00–11:30, position 15 = 06:30–07:00.
- `0xFFFE` = empty / no data.
- Per-block steps = M1[i] + M2[i] (both zeroed if 0xFFFE).

The history does **not** update live during the day — walks accumulate in the front records and the live total. The M1/M2 arrays are populated at day-end (likely around the 6:30 PM auto-sync, which coincides with the end of the 06:00–18:00 window). On rollover, the previous day's populated arrays appear in the yesterday section.

### Distance Record Stack (@246)

Absolute byte 246 holds **today's** stack of **u16 committed hourly distances** (meters), newest first. Empty before the first walk commits, then grows by right-shifting: when a new hour commits, its distance is inserted at @246 and the existing entries shift right 2 bytes — `[572]` → `[838, 572]` → `[789, 838, 572]` → … Each entry is the distance walked in that committed hour (total distance minus pending distance at commit time).

This region overlaps with the yesterday M2 tail in the rollover state — today's live distance data overwrites yesterday's frozen M2 as the day advances. This is by design, not corruption: the watch prioritizes today's live data. Yesterday's own distance stack is preserved separately (see Remaining Buffer Regions below).

### Remaining Buffer Regions

**@256–313** — **Yesterday's committed distance stack.** In the rollover state this region holds the previous day's per-hour distance values (newest first), preserved after midnight. The whole @246–313 area behaves as a right-shifting register: today's newest distance inserts at @246, and everything downstream (today's remaining entries, the zero gap, and yesterday's stack) shifts right 2 bytes per committed hour. Yesterday's stack therefore sits after a zero gap following today's entries, and its position advances 2 bytes per hour. In the normal-day state the region is empty (zeros) because yesterday's per-hour detail is gone — only the summary total survives in the ring.

**@318–373** — Daily-summary ring (see key field reference). Holds 7 days of step/distance totals, newest first. The 1521/585 values seen in early dumps were simply the days_ago=2 slot (July 30's data). Each day's totals advance one slot per day until they fall off after 7 days.

### Open Questions

- **Intensity bucket mapping:** Which accelerometer patterns map to which of the 4 active buckets (0–3; bucket 4 is always zero)? Does the mapping vary by activity type?
- **Yesterday's distance stack offset:** Empirically, yesterday's committed distance stack begins at `262 + 2*(hour-6)` in the rollover state (verified across hours 6-15; by ~hour 17 today's data has overwritten it). The region behaves as a per-hour right-shifting register, but the fixed 262 baseline (the zero gap separating today's stack from yesterday's) is not yet derived from first principles.
- **Pending walk structure:** Why 3 × u16 instead of 5 × u16 like the front records? Does the watch only track 3 intensity buckets for pending walks?

### C-Style Struct Definition

```c
// Lifelog buffer, 400 bytes, little-endian
#pragma pack(push, 1)

// 10-byte front record: 5 intensity buckets per committed hour, newest first.
typedef struct {
    uint16_t intensity[5];       // bucket 0=light … 4=highest (4 unused on ABL-100; 3 is sparse)
} lifelog_front_record_t;

// Fixed tail: offsets 314–399.
typedef struct {
    uint32_t zero1;              // @314: always 0
    struct {
        uint32_t steps;
        uint32_t distance;
    } daily_summary[7];          // @318-373: 7-day ring, slot 0 = yesterday
    uint32_t total_steps;        // @374: live day counter (midnight→now)
    uint32_t total_distance;     // @378: live distance in meters (midnight→now)
    uint16_t pending_walk[3];    // @382: uncommitted walk, 3 intensity buckets
    uint32_t zero2;              // @388: always 0
    uint32_t pending_distance;   // @392: uncommitted walk distance (meters)
    uint32_t bcd_total;          // @396: total_steps in little-endian packed BCD
} lifelog_tail_t;

#pragma pack(pop)
```

### Slot Mapping (M1/M2 arrays)

```c
// slot = (array_index + 10) % 24
// clock = 06:00 + slot * 30 min
//   array_index 0  → 11:00-11:30 (slot 10)
//   array_index 15 → 06:30-07:00 (slot 1)
//   0xFFFE = empty / no data
int      slot   = (index + 10) % 24;
uint16_t steps  = m1_walking[index] + m2_running[index];
```

