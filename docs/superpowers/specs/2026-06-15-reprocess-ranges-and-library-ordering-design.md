# Reprocess ranges, library ordering, and a persistent notification tray

**Date:** 2026-06-15
**Status:** Approved (design)

## Problem

1. **Reprocess is list-only.** The Re-process modal accepts a typed list of Lab IDs
   (newline/comma separated). There is no way to reprocess a *range* of samples, and
   no preview of what will actually run.
2. **The sample library looks "disorganized / non-linear."** The left-pane list is
   meant to be chronological by *when the sample was run* (injection time). Root cause:
   `distill.cdf_metadata` only parses **ISO** injection timestamps. Agilent/Thermo ANDI
   `.CDF` files store `injection_date_time_stamp` as `YYYYMMDDHHMMSS±ZZZZ` (e.g.
   `20250224134500-0500`) or `DD-Mon-YYYY HH:MM:SS`. When parsing fails it silently
   falls back to the **file modified time** (copy/process time), scrambling the order.

## Goals

- Range-aware reprocess with a **search → preview → confirm** flow.
- Library ordered by true injection time, with a one-time **manual** fix for existing data.
- Missing/!-found Lab IDs surfaced in a **persistent notification tray** that survives
  server restarts and is dismissed manually.

## Non-goals

- No JS unit-test harness is added; testable logic stays in Python.
- §4 does **not** recompute distillation results — it only re-derives the injection
  timestamp column.

## Design

### 1. Robust injection-timestamp parsing — `distill.py`

Add a pure helper:

```
parse_injection_datetime(raw: str) -> datetime | None
```

Tries, in order: ISO (`datetime.fromisoformat`) → ANDI compact `%Y%m%d%H%M%S` with an
optional trailing `±HHMM`/`±HH:MM` zone → `%d-%b-%Y %H:%M:%S` → `%m/%d/%Y %H:%M:%S` →
`%Y-%m-%d %H:%M:%S`. Returns `None` if all fail.

`cdf_metadata` calls it and only falls back to `datetime.fromtimestamp(path.stat().st_mtime)`
when it returns `None`. Pure function ⇒ unit-tested directly.

### 2. Range-aware reprocess (search → preview → confirm)

Backend owns all expansion logic (single source of truth, TDD-friendly):

- `parse_reprocess_query(text) -> list[Token]` — splits on newline/comma; each token is
  either `{"kind": "single", "value": str}` or `{"kind": "range", "start": int, "end": int}`.
  Range separators: `to`, `-`, `:` (surrounded by optional whitespace). `start`/`end`
  parsed as integers; reversed ranges are normalised.
- `resolve_query(tokens, library_names) -> {"matched": [...], "missing": [...]}` —
  `matched` = library Lab IDs hit by a single token (exact match, with int fallback) or
  inside a range (the visible Lab ID parsed as int). `missing` = integers inside a range
  that match no library sample. Pure.
- `POST /api/reprocess/preview {query}` → `{matched, missing}` using the live file cache's
  Lab IDs. The frontend renders both lists.
- Confirm posts `matched` to the existing `POST /api/reprocess {samples}`. On confirm the
  `missing` IDs are written to the notification tray (§3).

Frontend: the Re-process modal becomes a query field + a debounced preview (matched
samples list, missing-IDs list) + a Confirm button (disabled when `matched` is empty).

### 3. Persistent notification tray — new module `notifications.py`

File-backed JSON store at `~/.gc_viewer_notifications.json`, lock-protected. Keeps
`app.py` (already ~150 KB) from growing.

- `add(level, message) -> entry`, `list_all() -> list`, `dismiss(id) -> bool`,
  `dismiss_all() -> int`. Entry: `{id, ts, level, message}` (`id` is a monotonic/uuid string).
- Routes: `GET /api/notifications`, `POST /api/notifications/<id>/dismiss`,
  `POST /api/notifications/dismiss-all`.
- Frontend: toolbar bell + unread badge, dropdown tray, per-item dismiss + "Dismiss all".
  Polled on the existing cadence.

### 4. Settings button — "Re-derive injection times & reorder library"

Manual, so the normal cache build stays fast.

- `POST /api/library/reindex-times` → runs on the existing background task queue (like
  `rebuild-db`).
- Backs up the CSV to `*.bak`, then for each row re-opens its `Source File` CDF, re-derives
  the injection time via §1, and rewrites **only** the `InjectionDateTime` column under
  `_CSV_LOCK`. Rebuilds the file cache so the library reorders immediately.
- Posts a summary to the notification tray (rows updated, unreadable CDFs).
- Note: `InjectionDateTime` is half the dedup key `(Lab ID, InjectionDateTime)`. Rewriting
  it is intentional; the whole CSV is rewritten consistently and backed up first.

## Implementation order (each TDD'd where logic is in Python)

1. §1 `parse_injection_datetime` + `cdf_metadata` wiring.
2. §3 `notifications.py` store.
3. §2 `parse_reprocess_query` + `resolve_query` + preview route.
4. §4 reindex route.
5. Frontend wiring for §2/§3/§4 (manual verification — no JS test harness).

## Testing

Python unit tests in `tests/`: timestamp parser, query parse/resolve, notifications store
(temp file). Route-surface assertions follow the existing AST-based `test_app_routes.py`
pattern where importing `app` has side effects. Frontend verified manually.
