# Phase 2: Interface contracts for parallel lanes

Companion to `2026-09-28-phase2-multi-instrument-hub-design.md` (rev 3).
Lanes are built in parallel against **these** interfaces. Any lane that needs
to change a contract must say so in its report. The integration critic checks
every seam listed here.

## Lanes

| Lane | Builds | Branch / worktree | Depends on |
|---|---|---|---|
| A | 2A0 → 2A1 → 2A2 (hub core) | `feat/phase2` | — |
| B | 2B2 agent package `agent/` (tested against a fake hub) | `lane/agent` | this doc §1 |
| C | 2C `corrections.py` provider + fake LEM; the LEM repo change | `lane/corrections`; LEM branch `gc-hub-upstream-corrections` | this doc §2 |
| D | 2D `import_match.py` matcher + dry-run report against the local snapshot | `lane/import` | this doc §3 |
| (later) | 2B1 ingest API, the hub wiring of C and D | `feat/phase2` after 2A1 | §1–§3 |

Lanes B, C and D create **new files only** in gc-hub. They must not edit
`app.py`, `distill.py`, `looker.py`, `settings.py` or `paths.py`. Wiring them
into the hub is Lane A's job after 2A1.

## §1 Hub ↔ agent HTTP contract (Lane B builds the client; 2B1 builds the server)

- **Base:** `hub_url`, e.g. `http://asapsv1:5560`.
- **Auth:** every request carries `Authorization: Bearer <token>`.
- **Responses:** every body is JSON. Errors are `{"error": "<human message>"}`.

### `POST /api/ingest`
- **Request headers:**
  - `X-GC-SHA256`: lower-case hex of the body.
  - `X-GC-Mtime`: ISO 8601 local time of the file's mtime, no timezone,
    `YYYY-MM-DDTHH:MM:SS`.
  - `X-GC-Filename`: `urllib.parse.quote(basename)`.
  - `Content-Type: application/octet-stream`.
- **Body:** raw CDF bytes, at most 25 MB.
- **Responses:**

| Status | Body | Agent action |
|---|---|---|
| 201 | `{"sample_id": int, "sha256": str, "status": str}` | mark sent (only if the returned `sha256` equals the one sent) |
| 200 | `{"sample_id": int, "sha256": str, "duplicate": true}` | mark sent |
| 202 | `{"sha256": str, "conflict_id": int}`, where `sha256` is the **received body's** sha | mark sent (the hub holds it for review) |
| 400, 409, 413, 415 | `{"error": str}` | mark **rejected** with the reason; retry only on `retry-rejected` |
| 401, 403, 404, 405, other 4xx | `{"error": str}` | **hold**: stop sending, tray red, keep the queue, retry every 300 s or on config change |
| 429, 5xx, network error | — | back off exponentially from 5 s up to 300 s |

### `POST /api/agent/heartbeat`
- **Request:**
  ```
  {"version": str, "package_sha256": str,
   "state": "sending"|"idle"|"paused"|"hub-unreachable"|"auth-error"|"config-error",
   "queue_size": int, "rejected_count": int,
   "last_file": str|null, "last_error": str|null,
   "host": str, "agent_time": "YYYY-MM-DDTHH:MM:SS", "results_seq": int}
  ```
- **Response 200:**
  ```
  {"command": null|"restart"|"pause"|"resume"|"retry-rejected"|"adopt-mirror",
   "agent_package_sha256": str, "server_time": str}
  ```
- A command is delivered once. The agent acts on it, and the next heartbeat
  reflects the new state.

### `GET /api/agent/results?after=<int seq>&limit=<int ≤ 500>`
- **Response 200:**
  `{"header": [31 CSV_HEADER names], "rows": [{"seq": int, "line": str}], "more": bool}`
- **`line`** is the exact CSV record, **including** its `\r\n` terminator,
  produced by `csv.writer` with default dialect. The agent writes
  `line.encode("utf-8")` verbatim.
- Rows are in ascending `seq` for the token's instrument only.

### `GET /api/agent/package` and `GET /api/agent/package.zip`
- `package` returns `{"version": str, "sha256": str}`, where `sha256` is the
  sha256 of the zip bytes.
- `package.zip` returns the zip.
  - Its root holds `agent_main.py`, the `gc_agent/` package, `VERSION` and
    `requirements-agent.txt` (informational only).
  - It never contains a token.

### Agent files (on the GC PC)
- **Root:** `%LOCALAPPDATA%\ASAPLabs\gc-agent\`, holding `launcher.pyw`,
  `versions\<v>\`, `current.txt`, `previous.txt`, `agent.json`, `ledger.db`
  and `agent.log`.
- **`agent.json`:**
  `{"hub_url", "token", "watch_dir", "include_subdirs": true, "poll_seconds": 5, "stable_seconds": 30, "results_mirror_path": "", "paused": false, "python": "<abs path to pythonw.exe>"}`.
- **Mirror sidecar (amended after the integration review):** `<mirror>.gcagent.json`, never the hub's `.gchub.json`. The agent refuses to mirror into any file that has a hub sidecar. Its temp files are named `.gcagent-<sidecar>.<rand>.part`. Fields:
  `{"size": int, "sha256": str, "seq": int, "adopted_at": str}`.
- **Launcher exit codes:** 0 = quit, 3 = restart (re-read `current.txt`),
  anything else = crash.
- **Other agent files** (amended after the Lane B review):
  `versions/<v>/PACKAGE_SHA256`, `revert.json`, `bad_packages.json`,
  `failed_packages.json`, `launcher.log`, `launcher.lock` (POSIX only). The
  mirror sidecar may briefly carry an extra `"pending"` key during an append;
  readers must tolerate it.
- **Installer download (2B1 produces it):** `install.pyw`, `launcher.pyw`,
  `install.json` = `{"hub_url", "token"}`, plus optional `agent-package.zip`
  and `agent-package.json` = `{"version", "sha256"}`. The hub builds the
  agent package with `agent/build_package.py`, which ships in releases.
- **Client-side rules:** files over 25 MB are rejected locally without being
  sent. A sha mismatch in a 200/201/202 body marks the file rejected. A
  non-JSON 2xx backs off. 3xx holds. Only the listed commands are obeyed.

## §2 Corrections (REWRITTEN 2026-09-28 for D4b: hub-owned; Lane C builds it, the Lane A pipeline calls it)

```python
# corrections.py
D86_CUTS = ["IBP","5%","10%","20%","30%","50%","70%","80%","90%","95%","FBP"]
PHASE1_FILE_MAP = {...}              # phase-1 JSON test_name -> cut; used only to seed gc1
MAX_ABS_CORRECTION_C = 50.0

@dataclass(frozen=True)
class Corrections:
    source: str        # 'hub' | 'file' | 'legacy'
    updated_at: str    # when these values were last changed (UTC ISO with offset; 'file' = file mtime)
    updated_by: str = ""
    values: dict       # cut -> float, all 11 D86_CUTS (explicit 0.0 allowed)

class CorrectionsUnavailable(Exception):   # sample -> pending_corrections
    reason: str; kind: str                 # kind == 'config'

class StoreProvider:
    def __init__(self, read_fn): ...       # read_fn(instrument_id) -> {values, updated_at, updated_by} | None
    def get(self, instrument: dict) -> Corrections: ...   # raises CorrectionsUnavailable on none/partial

def seed_from_file(json_path) -> dict: ...                 # 11-cut values for seeding gc1
def validate_values(values: dict) -> list[str]: ...        # editor validation
def values_differ(a: dict, b: dict) -> bool: ...           # missing cut counts as 0.0
```

The store (Lane A) owns the `instrument_corrections` and `corrections_audit`
tables and supplies `read_fn = store.corrections.read`:
- returns `None` when the instrument has no rows;
- otherwise returns `{values: {cut: float}, updated_at: <latest row's, UTC
  ISO with offset>, updated_by: <that row's>}`.

If `read_fn` raises (a database error), the pipeline leaves the sample as it
was and retries it; it is never marked `error`. The editor parses form input
into floats before calling `validate_values`.

## §3 History import matcher (AMENDED 2026-09-28 after the Lane D review; Lane D builds it, the Lane A importer and parity report call it)

```python
# import_match.py  (pure: no DB, no Flask; reads files only; has its own pinned v1 parser)
@dataclass(frozen=True)
class CdfMeta:
    path: str; sha256: str; lab_id: str
    injection_dt: str                 # CORRECT time, canonical naive isoformat(sep=" ")
    dt_source: str                    # 'cdf' | 'mtime'
    method_name: str = ""             # detection_method_name, trimmed, basename, upper; '' if absent
    legacy_injection_dt: str = ""     # what v1 wrote on py>=3.11 (fromisoformat-first bug)
    raw_stamp: str = ""
    v1_injection_dts: tuple = ()      # candidate strings v1 may have written (3.11+ form, correct form)

@dataclass(frozen=True)
class CsvRow:
    line_no: int; lab_id: str; injection_dt_raw: str; source_file: str; values: dict

@dataclass
class MatchedSample:
    cdf: CdfMeta | None               # None = result-only (legacy_unverified, dt_source 'csv')
    rows: list[CsvRow]                # CSV order; last = current revision; [] = orphan CDF
    # derived: lab_id (normalised), lab_id_display (verbatim), injection_dt (correct), dt_source,
    #          time_unverifiable (result-only rows whose CSV time could be a v1 misparse)

@dataclass
class MatchReport:
    samples: list[MatchedSample]
    unmatched_rows: list[CsvRow]
    dup_sha: list[tuple[str, str]]
    no_injection_time: list[str]
    mixed_rows: list[CsvRow]
    key_collisions: list[tuple[CdfMeta, CdfMeta]]   # (kept, other): store `other` as a conflict
    ambiguous_rows: list[tuple[CsvRow, CdfMeta, list[CdfMeta]]]  # one CSV key fitting several CDFs (row, chosen, others)
    held_rows: list[CsvRow]                         # short/long/decode-error rows: never imported blindly
    stats: dict   # method_names, v1_misparsed_cdfs, rows_matched_via_v1_form, collided_rows, near_misses, timezone, ...

def normalise_lab_id(s) -> str: ...   # strip only; keep the original for display
V1_FAMILIES = ('py311_313', 'py314')  # interpreter-independent emulation of v1's misparse
def _v1_parse(raw, family) -> datetime | None: ...
def read_cdf_meta(path) -> CdfMeta: ...
def read_results_csv_ex(path) -> (rows, issues): ...
def match(cdfs, rows, *, instrument_folder: str | Sequence[str], csv_issues=None) -> MatchReport: ...
def dry_run(processed_dir, results_csv=None, *, instrument_folder, on_progress=None) -> MatchReport: ...
```

- **Match rule:** normalised lab IDs are equal, **and** `injection_dt_raw`
  equals **any** of the CDF's `v1_injection_dts`, as an exact string. The
  hub stores `injection_dt` (the correct time), `legacy_injection_dt`, and
  `time_corrected` when the two differ.
- **Schema amendments:**
  - `samples.injection_dt_source` also allows `'csv'` (result-only imports).
  - `samples.time_unverifiable` flags result-only imports whose CSV time
    could be a v1 misparse.
  - `legacy_injection_dt` holds the py311_313 form.
  - Matching tries both families and the correct form.
