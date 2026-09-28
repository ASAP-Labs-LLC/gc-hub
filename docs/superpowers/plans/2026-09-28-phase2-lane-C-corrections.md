# Phase 2 / Lane C: corrections provider + the LEM upstream flag (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:test-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** (1) `corrections.py` in gc-hub, implementing contract §2 of
`docs/superpowers/specs/2026-09-28-phase2-contracts.md` exactly, with the
LEM rules of the design's "LEM corrections (2C)". (2) In LEM, a per-machine
flag `corrections_applied_upstream` that stops the station module adding
`lem_correction_factors` to rows the hub has already corrected (D4).

**Spec:** `2026-09-28-phase2-multi-instrument-hub-design.md` ("LEM corrections
(2C)", "Status machine", D4, D5) and the contracts doc §2.

## Conventions

- gc-hub branch `lane/corrections` (worktree under `.claude/worktrees/`).
  **New files only**: `corrections.py`, `tests/corrections/*`, this plan. No
  edits to `app.py`, `distill.py`, `settings.py`, `paths.py` or existing tests.
- Python: the main checkout's `/Users/rynatical/Projects/gc-hub/.venv/bin/python`
  (the worktree has no `.venv`).
- `tests/corrections/` has **no `__init__.py`**: with one, pytest's rootdir
  import would name the package `corrections` and shadow `corrections.py`.
  Test basenames are unique (`test_corrections_*.py`).
- TDD: failing test first (record the red output), then the code, then the
  full suite, then commit. Commit messages end with the Co-Authored-By line.
- LEM: read `ASK-CLAUDE.md` first. Reads of production LabCore are fine;
  **no writes, no tags, no push**. Work outside the user's LEM checkout.

## What LEM really answers (verified read-only)

`GET /api/machines/<uid>/corrections` (`LEM Web Server/web_app.py`
`api_get_corrections`) answers `200 {"corrections": [{"test_name", "correction",
"units"}], "methods": [str]}`; an unknown uid answers `200 {"corrections": [],
"methods": []}`; an unreadable LabCore answers `502`/`503` JSON via
`_labcore_unreadable`. `methods` = mapped methods from `lem_machine_config`
plus QC test names plus any name that already carries a correction.

## Part 1: gc-hub `corrections.py`

| Task | Test file | What |
|---|---|---|
| 1 | `test_corrections_map.py` | `D86_CUTS`, `DEFAULT_CORRECTION_MAP` (11 cuts, equal to `distill._D86_CORRECTION_TEST_MAP`), `Corrections` frozen dataclass, `CorrectionsUnavailable(reason, kind)` with kind validated, `parse_correction_map(None / JSON string)`: invalid JSON, non-object, empty, blank names, unknown cut, two names for one cut → `config`. |
| 2 | `test_corrections_file.py` | `FileProvider`: phase-1 shape; only instrument `gc1` (else `config`); missing file / bad JSON / missing or non-object section / section with no mapped test / bad `correction_value` → `config`; an OS error other than not-found → `unreachable`; cuts absent from the section → explicit 0.0 (phase-1 behaviour, see "Decisions"); custom map; `refresh == get`; `changed_since`. |
| 3 | `test_corrections_lem.py` | `LemProvider` with the injectable `http_get`: all 11 cuts; explicit zero (in methods, not in corrections); unknown machine (`methods` empty) → `config`; name in neither → `config`; units `°C`/`C`/`""` ok, anything else → `config` (mapped tests only); non-numeric correction → `config`; non-JSON 200 / HTML content type / timeout / connection error / 5xx / 429 → `unreachable`; other 4xx and a malformed JSON shape → `config`; URL quoting of the uid; missing uid → `config`. |
| 4 | `test_corrections_lem.py` | Cache: a successful fetch is saved to the store; unreachable + cache age exactly 24 h → `source='cache'` with the cached `fetched_at`; 24 h + 1 s → raise `unreachable`; no cache → raise; a cache whose cuts don't match the current map is not used; a `config` error never falls back to the cache; `store.load` raising = no cache; `store.save` raising doesn't fail a good fetch. |
| 5 | `test_corrections_lem.py` | Freshness: second `get` within 300 s makes no HTTP call; at 300 s it refetches; store freshness survives a new provider (restart); a changed map or uid bypasses it; an unreachable outcome is remembered for 300 s (no 5 s timeout per sample in a backlog) and still answers from the cache; `refresh()` always fetches. |
| 6 | `test_corrections_lem.py` | `changed_since(instrument_id, used)`: False with no fetch yet, False when equal, True when any value differs or the cut set differs; compares against the latest fetch in the store. |
| 7 | `test_corrections_fake_lem_server.py` | A real `http.server` thread serving LEM's exact JSON, driven through the **default** (`requests`) `http_get`: happy path, HTML sign-in page, 503 `_labcore_unreadable` body with cache fallback, timeout (server sleeps past a 0.3 s timeout), connection refused, custom `correction_map` JSON string. |

## Part 2: LEM `corrections_applied_upstream`

Where it flows: the station's `Machine` dataclass is the per-machine config;
`Machine.to_dict()` is published to `lem_machine_config.config`
(`build_config_upsert`) and read back at bind (`machine_from_config_payload`).
The web API (`/api/machine-configs/<uid>`) stores the dict opaquely. Rows are
corrected at one place, `apply_row_corrections(rows, machine.corrections)` in
the poll.

| Task | Test | What |
|---|---|---|
| 8 | `LEM Station Module/tests/test_corrections_applied_upstream.py` | `Machine.corrections_applied_upstream` defaults False; round-trips `to_dict`/`from_dict`; an old config without the key loads False; a truthy-but-not-bool value (e.g. `"false"`) loads False (only real `True` turns corrections off). |
| 9 | same | `apply_row_corrections(rows, corrections, applied_upstream=True)` returns rows unchanged with no raw/correction keys; default unchanged; the poll call passes the machine's flag (source check + a behavioural test through the poll helper if one exists). |
| 10 | same | Treated as per-machine, not copyable: added to `CONFIG_RUNTIME_KEYS` (station) and `machine_configs.RUNTIME_KEYS` (server), so a duplicated config starts False; a self-save keeps it. |
| 11 | `test_module_qt.py`-style (skips without PySide6) | The machine config dialog shows a checkbox, loads and saves the flag. |
| 12 | — | Run the affected LEM test files + all station module tests + the web server suite. Commit on `gc-hub-upstream-corrections`. No push, no tag. |

Stop rule: if the change needs more than a flag + guard (+ the dialog
checkbox), write a proposal instead.

## Decisions (explicit)

- **File provider, absent cuts = 0.0.** `distill.load_d86_corrections`
  returns only the cuts the file lists and `apply_d86_corrections` adds
  `corrections.get(k, 0.0)`, so today an absent cut is uncorrected. V4 held
  five Agilent offsets, so the file very likely lists five. Raising on absent
  cuts would put every 2A1 parity sample into `pending_corrections`. What
  phase 1 hid is now a raise: missing/unparseable file, missing section, a
  section with no mapped test, or an unparseable mapped value.
- **Cache entry shape** (`load()` return): `{"values": {cut: float},
  "methods": [str], "fetched_at": str}`. Values are per cut, as used.
- **Timestamps** are naive local ISO (`timespec="seconds"`), like the rest of
  the hub.
- **Extension:** `LemProvider(..., clock=None)` and `FileProvider(..., clock=None)`
  take an optional clock for tests. `changed_since` is a provider method.
