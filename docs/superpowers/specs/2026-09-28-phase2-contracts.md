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
| 202 | `{"sha256": str, "conflict_id": int}` | mark sent (the hub holds it for review) |
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
- **Mirror sidecar:** `<mirror>.gchub.json` =
  `{"size": int, "sha256": str, "seq": int, "adopted_at": str}`.
- **Launcher exit codes:** 0 = quit, 3 = restart (re-read `current.txt`),
  anything else = crash.

## §2 Corrections (REWRITTEN 2026-09-28 for D4b: hub-owned; Lane C builds it, the Lane A pipeline calls it)

```python
# corrections.py
D86_CUTS = ["IBP","5%","10%","20%","30%","50%","70%","80%","90%","95%","FBP"]
PHASE1_FILE_MAP = {...}              # phase-1 JSON test_name -> cut; used only to seed gc1
MAX_ABS_CORRECTION_C = 50.0

@dataclass(frozen=True)
class Corrections:
    source: str        # 'hub' | 'file' | 'legacy'
    updated_at: str    # when these values were last changed
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
tables and supplies `read_fn`.

## §3 History import matcher (Lane D builds it; the Lane A importer job calls it)

```python
# import_match.py  (pure: no DB, no Flask; reads files only)
@dataclass(frozen=True)
class CdfMeta:
    path: str; sha256: str; lab_id: str; injection_dt: str   # canonical naive isoformat(sep=" ")
    dt_source: str                                           # 'cdf' | 'mtime'
    method_name: str                                         # detection_method_name, trimmed, basename, upper-cased; '' if absent
    legacy_injection_dt: str                                 # what v1 wrote (fromisoformat-first bug on py>=3.11); == injection_dt when unaffected

@dataclass(frozen=True)
class CsvRow:
    line_no: int; lab_id: str; injection_dt_raw: str; source_file: str; values: dict  # CSV_HEADER -> str

@dataclass
class MatchedSample:
    cdf: CdfMeta | None                 # None = result-only (legacy_unverified)
    rows: list[CsvRow]                  # CSV order; last = current revision; may be empty (orphan CDF)

@dataclass
class MatchReport:
    samples: list[MatchedSample]
    unmatched_rows: list[CsvRow]        # also represented as result-only samples above; listed for review
    dup_sha: list[tuple[str, str]]      # (path, path) identical bytes within the folder
    no_injection_time: list[str]        # CDF paths that needed the mtime fallback
    mixed_rows: list[CsvRow]            # rows whose CDF lives outside this instrument's folder
    stats: dict                         # includes 'method_names': {name: count} ('' = absent)

def read_cdf_meta(path) -> CdfMeta: ...
def read_results_csv(path) -> list[CsvRow]: ...
def match(cdfs: list[CdfMeta], rows: list[CsvRow], *, instrument_folder: str) -> MatchReport: ...
def dry_run(processed_dir, results_csv, *, instrument_folder) -> MatchReport: ...   # plus a text summary CLI
```

- **Match rule:** a row attaches to a CDF only when the lab IDs are equal
  (stripped) **and** `injection_dt_raw` equals the CDF's
  **`legacy_injection_dt`** (canonical string equal; see spec "Injection
  time"). `injection_dt` is the corrected time that gets stored. `Source File` prefix matches are never trusted
  on their own. Filenames are never used for identity.
