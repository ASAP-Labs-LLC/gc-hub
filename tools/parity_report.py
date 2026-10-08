#!/usr/bin/env python3
"""The v1 parity report (phase 2, 2A1 T6): the 2A1 go-live gate.

The report is driven by the **hub's samples** for one instrument (optionally
a scope: the sample ids a folder load produced, or those received since a
time). Each hub sample is paired with v1's results CSV rows by lab ID and
injection time (the correct time or v1's legacy string); its current
revision is compared with v1's last row for it, all 31 ``CSV_HEADER``
columns as exact strings (the hub side is ``exports.format_line``, what the
hub exports).

An explanation is **claimed only when proven**: the sample's recorded CDF is
recomputed (``distill.compute``, as v1 ran it: non-strict blank) with the
revision's recorded calibration anchors (or v1's auto-detected calibration),
v1's corrections file, and the blank v1 would have used; the tag is given
only if a recomputed row equals v1's result columns string for string, and
the detail names what reproduced it. Before any of that, recomputing with the
revision's own inputs must reproduce the revision (the control); otherwise
nothing can be proven.

Tags. **Failing** tags fail the report; the others are informational.

====================  =======  ==============================================
``source-file``       info     the hub records its relative CDF path, v1 an
                               absolute one
``injection-time-fix`` info    v1's ``InjectionDateTime`` is the sample's
                               ``legacy_injection_dt`` (v1's stamp misparse,
                               or an unrounded file time)
``blank-rule``        info     proven: v1's blank (newest registered, replayed
                               from the CSV; a ``(Blank)``/``[b] Blank`` sample
                               v1 blank-subtracted; or, when v1's blank is
                               unknown, a genuine blank of this instrument
                               within ``blank_window_days``) reproduces v1
``auto-detect-off``   info     proven: v1's auto-detected calibration
                               reproduces v1 (the hub holds such samples
                               ``awaiting_calibration`` or uses the assigned
                               peaks)
``corrections``       info     proven: v1's corrections file (with the hub's
                               blank and calibration) reproduces v1
``d86-monotonic``     info     v7.0.0: v1's corrected D86 dipped below an
                               earlier cut there; the hub holds it at the
                               highest earlier v1 value (proven on the two
                               rows: only those D86 cells, the hub's equal to
                               its previous D86 cell, its series never dips).
                               Recomputations are compared with v1's row
                               under the same rule
``method-excluded``   info     the hub doesn't process the CDF's method
                               (``other_method``) and the name was accepted
                               (``accept_excluded_methods`` /
                               ``--accept-excluded-method``); never a D2887 name
``method-not-accepted`` FAIL   the same with a name not accepted (or a D2887
                               name, which can't be accepted)
``review-method``     FAIL     the CDF has no method name (held for review)
``not-verified``      FAIL     compared, but no numeric column was checked (the
                               v1 header lacked them), or the v1 row is
                               malformed (long-row, short-row, decode-error)
``not-in-hub``        info     a v1 row no hub sample matches (its CDF wasn't
                               loaded)
``v1-short-row``      info     a column the v1 header in effect for that row
                               lacked (older layouts)
``no-v1-row``         FAIL     a hub sample in scope with no v1 row
``lab-id-other-time`` FAIL     ...and v1 has rows for that lab ID at other times
``ambiguous-match``   FAIL     a v1 row matching more than one hub sample
``not-processed``     FAIL     the hub sample has no result yet
``unexplained``       FAIL     anything else, including a result column empty
                               on one side
====================  =======  ==============================================

The report also **fails** when no row was numerically verified (a hub result
whose numeric columns agree with v1's or differ only in proven ways), or when
a method name mapped to D2887 (or ``SIMDISB.M``/``SIMDISTB.M``) is among the
excluded. v1 rows superseded by a later row for the same hub sample are
counted, not compared, but still replayed for v1's blank.

::

    parity_report(instrument_id, v1_results_csv, *, db, out_dir, data_dir=None,
                  conf=None, v1_corrections=None, sample_ids=None, since=None,
                  blank_window_days=7, max_blank_candidates=6)
        -> {"summary", "differences", "csv", "html", "exit_code"}

``conf`` is the global settings the hub computes with (default
``settings.load_settings()``). ``v1_corrections`` is v1's
``correction_factors_json`` (a path, read as v1 read it, or a ``{cut:
value}`` dict); ``ValueError`` if it yields no cuts. Without it no
recomputation is attempted and differing numbers are ``unexplained``.
Written to ``out_dir``: ``parity-<instrument>-<stamp>.csv`` (every
difference) and ``.html`` (a self-contained summary). ``exit_code`` 1 = FAIL.

CLI::

    python tools/parity_report.py --data-dir C:\\ASAPApps\\gc\\data \\
        --v1-corrections "\\\\asapserver\\...\\correction_factors.json" \\
        --sample-ids @D:\\load-summary.json gc1 D:\\robocopy\\distill_results.csv

``--sample-ids`` takes ``1,2,3`` or ``@file`` (a loader ``--json`` summary,
or ids separated by commas/whitespace); ``--since`` an ISO time. Exit codes:
0 PASS; 1 FAIL; 2 bad arguments (missing CSV, data folder, database or
corrections file, or a corrections file with no cuts).
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import distill  # noqa: E402
import exports  # noqa: E402
import import_match  # noqa: E402
import instruments  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402

INFO_TAGS = ("source-file", "injection-time-fix", "blank-rule", "auto-detect-off", "corrections",
             "d86-monotonic", "method-excluded", "not-in-hub", "v1-short-row")
FAILING_TAGS = ("no-v1-row", "lab-id-other-time", "ambiguous-match", "not-processed",
                "review-method", "method-not-accepted", "not-verified", "unexplained")
TAGS = INFO_TAGS + FAILING_TAGS
PROVEN_TAGS = ("blank-rule", "auto-detect-off", "corrections")
TAG_HELP = {
    "source-file": "Expected: the hub records its own relative CDF path, v1 the absolute path.",
    "injection-time-fix": "v1 wrote the sample's legacy time (stamp misparse or unrounded file "
                          "time); the hub stores the correct time.",
    "blank-rule": "Proven by recomputation: v1's blank (not the hub's at-or-before blank) "
                  "reproduces v1's values.",
    "auto-detect-off": "Proven by recomputation: v1's auto-detected calibration reproduces "
                       "v1's values.",
    "corrections": "Proven by recomputation: v1's corrections file reproduces v1's values.",
    "d86-monotonic": "v7.0.0: v1's corrected D86 fell below an earlier cut here; the hub holds "
                     "it at that earlier value (a corrected D86 never decreases).",
    "method-excluded": "The CDF's method isn't processed by the hub, and its name was accepted "
                       "(--accept-excluded-method).",
    "method-not-accepted": "The CDF's method isn't processed by the hub and its name wasn't "
                           "accepted: its numbers were never compared.",
    "review-method": "The CDF has no method name; the hub holds it for review, unprocessed.",
    "not-verified": "Compared, but no numeric column could be checked (the v1 header lacked them "
                    "or the v1 row is malformed).",
    "not-in-hub": "A v1 row no hub sample matches (its CDF wasn't loaded). Informational.",
    "v1-short-row": "A column the v1 header in effect for that row didn't have.",
    "no-v1-row": "A hub sample with no v1 row: nothing to compare it with.",
    "lab-id-other-time": "A hub sample with no v1 row, while v1 has that lab ID at other times.",
    "ambiguous-match": "A v1 row that matches more than one hub sample.",
    "not-processed": "The hub sample has no result yet: run the report when the queue is empty.",
    "unexplained": "Nothing proven explains it.",
}
D2887_NAMES = frozenset(instruments.DEFAULT_METHOD_MAP)
MALFORMED_ISSUES = ("long-row", "short-row", "decode-error")
OUT_COLUMNS = ("v1_line", "lab_id", "v1_injection_dt", "sample_id", "hub_status", "column", "v1",
               "hub", "tag", "detail")
RESULT_COLUMNS = tuple(c for c in distill.CSV_HEADER
                       if c.startswith(("2887 ", "D86 ")) or c in ("Best Fit", "Fit Score"))
NUMERIC_COLUMNS = tuple(c for c in RESULT_COLUMNS if c != "Best Fit")
ALL_COLUMNS = frozenset(distill.CSV_HEADER)

# v1's blank-name rule (looker.Looker._BLANK_NAME, phase 1): "Blank", "blank2",
# "blank_3"; not "(Blank)" or "[b] Blank", which v1 blank-subtracted.
_V1_BLANK_NAME = re.compile(r"^blank[\s_\-]*\d*$", re.IGNORECASE)


def v1_blank_name(name: str) -> bool:
    return bool(_V1_BLANK_NAME.match((name or "").strip()))


def _parse_dt(text: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((text or "").strip())
    except ValueError:
        return None


def load_v1_corrections(v1_corrections) -> Optional[dict]:
    """``None`` stays ``None``; a dict is used as is; a path is read the way v1
    read it (``distill.load_d86_corrections``). ``ValueError`` if that yields
    no cuts (a missing or wrong file: v1 would have applied none, but a gate
    can't tell that from a typo)."""
    if v1_corrections is None:
        return None
    if isinstance(v1_corrections, dict):
        values = {k: float(v) for k, v in v1_corrections.items()}
    else:
        values = distill.load_d86_corrections(v1_corrections)
    if not values:
        raise ValueError(f"v1's corrections file {v1_corrections} yields no correction cuts "
                         f"(missing, unreadable or without an 'Agilent GC' section)")
    return values


D86_COLUMNS = tuple(c for c in distill.CSV_HEADER if c.startswith("D86 "))   # IBP → FBP


def v7_d86(values: dict) -> dict:
    """``values`` (column -> string) under v7.0.0's rule
    (``distill.monotonic_d86``): each D86 cell below an earlier D86 cell
    becomes the highest earlier cell's string; nothing else changes."""
    out = dict(values)
    best = None
    for c in D86_COLUMNS:
        v = out.get(c, "")
        if v == "":
            continue
        if best is not None and float(v) < best[0]:
            out[c] = best[1]
        else:
            best = (float(v), v)
    return out


def d86_held(v1: dict, hub: dict, differing) -> set:
    """The D86 columns of ``differing`` that v7.0.0's rule explains, all or
    none: every one is a cell where v1's corrected series dipped, the hub's
    value equals the hub's previous D86 cell and the highest earlier v1 cell,
    every v1 dip differs, and the hub's D86 series never dips."""
    cols = {c for c in differing if c in D86_COLUMNS}
    if not cols:
        return set()
    v1_best = hub_best = hub_prev = None
    dips = set()
    for c in D86_COLUMNS:
        a, b = v1.get(c, ""), hub.get(c, "")
        if a != "":
            if v1_best is not None and float(a) < v1_best:
                dips.add(c)
                if b == "" or b != hub_prev or float(b) != v1_best:
                    return set()
            v1_best = float(a) if v1_best is None else max(v1_best, float(a))
        if b != "":
            if hub_best is not None and float(b) < hub_best:
                return set()
            hub_best = float(b) if hub_best is None else max(hub_best, float(b))
            hub_prev = b
    return cols if dips == cols else set()


def _rendered(row: dict) -> dict:
    """A results row as the export line renders it (column -> string)."""
    line = exports.format_line(row, row.get("Source File", ""))
    return dict(zip(distill.CSV_HEADER, next(csv.reader(io.StringIO(line)))))


def _row_columns(path) -> dict:
    """``{line_no: frozenset(columns)}``: the columns the header in effect for
    each v1 data row had (``import_match.read_results_csv_ex``'s rules)."""
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8", errors="surrogateescape")
    reader = csv.reader(io.StringIO(text, newline=""))
    current = list(distill.CSV_HEADER)
    out, prev_end = {}, 0
    for cells in reader:
        line_no = prev_end + 1
        prev_end = reader.line_num
        if cells:
            cells[0] = cells[0].lstrip("\ufeff")
        if not any(c.strip() for c in cells):
            continue
        if cells[0].strip() == "Lab ID":
            current = [c.strip() for c in cells]
            continue
        cols = current
        if (len(cells) != len(current) and len(cells) == len(distill.CSV_HEADER)
                and current != list(distill.CSV_HEADER)):
            cols = list(distill.CSV_HEADER)
        out[line_no] = frozenset(c for c in cols if c) & ALL_COLUMNS
    return out


class _V1Blank:
    """v1's reference blank while the CSV is replayed in order: ``known`` (a hub
    sample id or None) or not, and the registered blank's injection time."""

    def __init__(self):
        self.known = False            # v1 may have started with a cached blank
        self.sample_id: Optional[int] = None
        self.dt: Optional[datetime] = None

    def copy(self) -> "_V1Blank":
        c = _V1Blank()
        c.known, c.sample_id, c.dt = self.known, self.sample_id, self.dt
        return c

    def register(self, row: import_match.CsvRow, hub: Optional[dict]) -> None:
        cand = _parse_dt(row.injection_dt_raw)
        newer = self.dt is None or cand is None or not self.dt > cand
        if hub is None:               # not (uniquely) in the hub: genuine or not is unknown
            if newer:
                self.known, self.sample_id = False, None
                self.dt = cand if cand is not None else self.dt
            return
        if not hub.get("is_blank"):   # v1 refused a blank-named run with sample signal
            return
        if newer:
            self.known, self.sample_id, self.dt = True, hub["id"], cand


def _since_utc(since) -> Optional[str]:
    if since is None:
        return None
    if isinstance(since, str):
        text = since.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        since = datetime.fromisoformat(text)
    if since.tzinfo is None:
        since = since.astimezone()
    return since.astimezone(timezone.utc).isoformat(timespec="microseconds")


class _Report:
    def __init__(self, instrument_id, v1_csv, *, db, data_dir, conf, v1_corr, sample_ids, since,
                 blank_window_days, max_blank_candidates, accepted=()):
        self.inst_id = instrument_id
        self.accepted = frozenset(n for n in (distill.normalise_method_name(a) for a in accepted) if n)
        self.scope_info = {"sample_ids": None if sample_ids is None else len(set(sample_ids)),
                           "since": None if since is None else str(since)}
        self.v1_csv = v1_csv
        self.db = db
        self.data_dir = Path(data_dir)
        self._conf = conf
        self.v1_corr = v1_corr
        self.window = timedelta(days=blank_window_days)
        self.max_blanks = max_blank_candidates
        self.differences: list = []
        self.verified = 0
        self.compared = 0
        self.compared_ids: set = set()
        self.method_names: dict = {}
        with store.connection(db) as conn:
            self.inst = store.instruments.get(instrument_id, db=conn) or {"id": instrument_id}
            self.open_conflicts = [
                {k: c[k] for k in ("id", "lab_id", "injection_dt", "existing_sample_id", "cdf_path",
                                   "received_at")}
                for c in store.conflicts.list(instrument_id, unresolved_only=True, db=conn)]
            self.samples = [dict(r) for r in conn.execute(
                "SELECT * FROM samples WHERE instrument_id=? AND cdf_path IS NOT NULL ORDER BY id",
                (instrument_id,))]
        self.by_id = {s["id"]: s for s in self.samples}
        cutoff = _since_utc(since)
        ids = None if sample_ids is None else {int(i) for i in sample_ids}
        self.scope = [s for s in self.samples
                      if (ids is None or s["id"] in ids)
                      and (cutoff is None or (s["received_at"] or "") >= cutoff)]
        self.scope_ids = {s["id"] for s in self.scope}
        self.blanks = sorted((s for s in self.samples if s["is_blank"]),
                             key=lambda s: s["injection_dt"])

    @property
    def conf(self) -> dict:
        if self._conf is None:
            import settings
            self._conf = settings.load_settings()
        return self._conf

    # ── pairing ────────────────────────────────────────────────────────────
    def run(self):
        rows, self.issues = import_match.read_results_csv_ex(self.v1_csv)
        self.rows = rows
        self.malformed: dict = {}
        for i in self.issues:
            if i["kind"] in MALFORMED_ISSUES:
                self.malformed.setdefault(i["line_no"], []).append(i["kind"])
        columns = _row_columns(self.v1_csv)
        by_key: dict = {}
        for s in self.samples:
            for dt in {s["injection_dt"], s.get("legacy_injection_dt") or s["injection_dt"]}:
                by_key.setdefault((s["lab_id"], dt), []).append(s)
        v1_times_by_lab: dict = {}
        rows_of: dict = {}                      # sample id -> [row index]
        claimed: set = set()
        state = _V1Blank()
        states: dict = {}
        self.not_in_hub = self.out_of_scope = 0
        for i, r in enumerate(rows):
            v1_times_by_lab.setdefault(r.lab_id, []).append(r.injection_dt_raw)
            cands = by_key.get((r.lab_id, r.injection_dt_raw), [])
            unique = cands[0] if len(cands) == 1 else None
            states[i] = state.copy()
            if not cands:
                self.not_in_hub += 1
                self._diff(r, None, "", "", "", "not-in-hub",
                           "no hub sample has this lab ID and injection time (correct or v1 form)")
            elif unique is None:
                in_scope = [c for c in cands if c["id"] in self.scope_ids]
                if in_scope:
                    claimed.update(c["id"] for c in in_scope)
                    self._diff(r, in_scope[0], "*", "", "", "ambiguous-match",
                               "matches hub samples " + ", ".join(str(c["id"]) for c in cands))
                else:
                    self.out_of_scope += 1
            elif unique["id"] in self.scope_ids:
                rows_of.setdefault(unique["id"], []).append(i)
            else:
                self.out_of_scope += 1
            if v1_blank_name(r.lab_id):
                state.register(r, unique)
        self.superseded = sum(len(v) - 1 for v in rows_of.values())
        for s in self.scope:
            idx = rows_of.get(s["id"])
            if idx:
                self.compared += 1
                self.compared_ids.add(s["id"])
                last = idx[-1]
                self._compare(rows[last], s, states[last], columns.get(rows[last].line_no, ALL_COLUMNS))
            elif s["id"] not in claimed:
                others = [t for t in v1_times_by_lab.get(s["lab_id"], [])]
                if others:
                    shown = ", ".join(sorted(set(others))[:5])
                    self._diff(None, s, "*", "", s["injection_dt"], "lab-id-other-time",
                               f"v1 has {s['lab_id']!r} only at other times: {shown}")
                else:
                    self._diff(None, s, "*", "", s["injection_dt"], "no-v1-row",
                               "no v1 row for this hub sample")
        return self

    def _diff(self, row, sample, column, v1, hub, tag, detail):
        self.differences.append({
            "v1_line": row.line_no if row is not None else "",
            "lab_id": row.lab_id if row is not None else sample["lab_id"],
            "v1_injection_dt": row.injection_dt_raw if row is not None else "",
            "sample_id": sample["id"] if sample is not None else "",
            "hub_status": sample["status"] if sample is not None else "",
            "column": column, "v1": v1, "hub": hub, "tag": tag, "detail": detail})

    # ── one sample ─────────────────────────────────────────────────────────
    def _compare(self, row, sample: dict, v1_state: _V1Blank, present: frozenset) -> None:
        rev = (store.get_revision(sample["id"], db=self.db)
               if sample["current_revision"] is not None else None)
        if rev is None:
            self._no_result(row, sample, v1_state, present)
            return
        hub = _rendered(dict(json.loads(rev["results"]), **{"Source File": sample["cdf_path"]}))
        result_diffs, failing_result = [], False
        numeric_checked = False
        for col in distill.CSV_HEADER:
            a, b = row.values.get(col, ""), hub.get(col, "")
            if col not in present:
                if a != b:
                    self._diff(row, sample, col, a, b, "v1-short-row",
                               "the v1 header in effect for this row has no such column")
                continue
            if col in NUMERIC_COLUMNS and a != "" and b != "":
                numeric_checked = True
            if a == b:
                continue
            if col == "Source File":
                self._diff(row, sample, col, a, b, "source-file",
                           "hub-relative path vs v1's absolute path")
            elif col == "InjectionDateTime":
                if a == (sample.get("legacy_injection_dt") or "") and a != sample["injection_dt"]:
                    why = ("v1 wrote the unrounded file time" if sample["injection_dt_source"] == "mtime"
                           else "v1 misparsed the injection stamp")
                    self._diff(row, sample, col, a, b, "injection-time-fix", why)
                else:
                    self._diff(row, sample, col, a, b, "unexplained", "injection time differs")
            elif col in RESULT_COLUMNS:
                if a == "" or b == "":
                    failing_result = True
                    self._diff(row, sample, col, a, b, "unexplained",
                               "empty on one side: nothing can explain a missing value")
                else:
                    result_diffs.append((col, a, b))
            else:
                self._diff(row, sample, col, a, b, "unexplained", f"{col} differs")
        if result_diffs:
            held = d86_held({c: row.values.get(c, "") for c in D86_COLUMNS if c in present}, hub,
                            [c for c, _a, _b in result_diffs])
            for col, a, b in result_diffs:
                if col in held:
                    self._diff(row, sample, col, a, b, "d86-monotonic",
                               "v1's corrected D86 dipped below an earlier cut; the hub holds it at "
                               "that cut's value")
            result_diffs = [d for d in result_diffs if d[0] not in held]
        if result_diffs:
            tag, detail = self._prove(row, sample, rev, v1_state, present)
            for col, a, b in result_diffs:
                self._diff(row, sample, col, a, b, tag or "unexplained", detail)
            failing_result = failing_result or tag is None
        why = []
        if not numeric_checked:
            why.append("no numeric column could be compared (the v1 header in effect lacks them, "
                       "or the values are empty)")
        if row.line_no in self.malformed:
            why.append(f"the v1 row is malformed ({', '.join(self.malformed[row.line_no])})")
        if why:
            self._diff(row, sample, "*", "", "", "not-verified", "; ".join(why))
        elif not failing_result:
            self.verified += 1

    def _no_result(self, row, sample, v1_state, present) -> None:
        status = sample["status"]
        if status in ("other_method", "review_method"):
            name = distill.normalise_method_name(sample.get("method_name"))
            self.method_names[name or "(none)"] = self.method_names.get(name or "(none)", 0) + 1
            if status == "review_method" or not name:
                self._diff(row, sample, "*", "", "", "review-method",
                           "the CDF has no method name; held for review and never processed")
            elif name in self.accepted and not self._is_d2887(name):
                self._diff(row, sample, "*", "", "", "method-excluded",
                           f"hub status {status}, method {name} (accepted as not D2887)")
            else:
                why = ("a D2887 method name: it must be mapped, not excluded" if self._is_d2887(name)
                       else "not accepted: pass --accept-excluded-method " + name
                       + " if it really isn't a D2887 run")
                self._diff(row, sample, "*", "", "", "method-not-accepted",
                           f"hub status {status}, method {name}; {why}")
        elif status == "awaiting_calibration":
            tag, detail = self._prove(row, sample, None, v1_state, present)
            if tag == "auto-detect-off":
                self._diff(row, sample, "*", "", "", tag, detail)
            else:
                self._diff(row, sample, "*", "", "", "unexplained",
                           f"hub status awaiting_calibration; {detail}")
        elif status == "error":
            self._diff(row, sample, "*", "", "", "unexplained",
                       f"hub status error: {sample.get('error') or ''}".strip())
        else:
            self._diff(row, sample, "*", "", "", "not-processed", f"hub status {status}")

    def _is_d2887(self, name: str) -> bool:
        return (name in D2887_NAMES
                or instruments.method_map(self.inst).get(name) == instruments.DEFAULT_METHOD)

    # ── proof by recomputation ─────────────────────────────────────────────
    def _cdf(self, sample: dict, rev: Optional[dict]) -> Path:
        return self.data_dir / ((rev or {}).get("cdf_path") or sample["cdf_path"])

    def _blank_path(self, blank_id: Optional[int]) -> Optional[Path]:
        if blank_id is None:
            return None
        b = self.by_id.get(blank_id) or store.samples.get(blank_id, db=self.db)
        return self.data_dir / b["cdf_path"] if b and b.get("cdf_path") else None

    def _calibration_confs(self, rev: Optional[dict]) -> list:
        """[(label, conf, allow_auto)]: the revision's recorded anchors, then v1's
        auto-detection on the same calibration CDF."""
        out = []
        cal_cdf = None
        if rev is not None:
            cal = json.loads(rev.get("calibration_used") or "null") or {}
            cal_cdf = cal.get("cdf")
            if cal_cdf and cal.get("anchors"):
                c = dict(self.conf)
                entries = [{"rt": float(rt), "carbon": int(carbon)} for rt, carbon in cal["anchors"]]
                c["calibration_cdf"] = cal_cdf
                c["calibration_assignments"] = distill.upsert_assignments("", Path(cal_cdf), entries)
                c["calibration_sensitivity"] = str(cal.get("sensitivity", 50))
                out.append(("the revision's calibration", c, False))
        if not cal_cdf:
            ctx = instruments.context(self.inst, self.conf, data_dir=self.data_dir)
            cal_cdf = ctx.get("calibration_cdf") or self.conf.get("calibration_cdf") or ""
            sens = ctx.get("calibration_sensitivity")
        else:
            sens = str((json.loads(rev.get("calibration_used") or "{}") or {}).get("sensitivity", 50))
        if cal_cdf:
            c = dict(self.conf)
            c.update(calibration_cdf=cal_cdf, calibration_assignments="",
                     calibration_sensitivity=str(sens or 50))
            out.append((f"v1's auto-detected calibration ({Path(cal_cdf).name})", c, True))
        return out

    def _recompute(self, cdf: Path, conf: dict, blank_id, corrections: dict,
                   allow_auto: bool) -> Optional[dict]:
        try:
            result = distill.compute(cdf, conf, self._blank_path(blank_id), corrections=corrections,
                                     honour_env=False, allow_auto=allow_auto, strict_blank=False)
        except Exception:  # noqa: BLE001 - a candidate that can't run proves nothing
            return None
        return _rendered(dict(result["row"], **{"Source File": ""}))

    def _blank_candidates(self, sample: dict, v1_state: _V1Blank) -> list:
        """[(blank sample id or None, description)] v1 may have subtracted."""
        lab = sample["lab_id"]
        if v1_blank_name(lab):
            return [(None, "no blank (v1 never blank-subtracted a blank name)")]
        if v1_state.known:
            desc = (f"blank sample {v1_state.sample_id} (v1's newest registered blank)"
                    if v1_state.sample_id is not None else "no blank (v1 had none registered)")
            return [(v1_state.sample_id, desc)]
        at = _parse_dt(sample["injection_dt"])
        near = [b for b in self.blanks if b["id"] != sample["id"]
                and at is not None and _parse_dt(b["injection_dt"]) is not None
                and abs(_parse_dt(b["injection_dt"]) - at) <= self.window]
        near.sort(key=lambda b: abs(_parse_dt(b["injection_dt"]) - at))
        return ([(None, "no blank (v1's blank unknown)")]
                + [(b["id"], f"blank sample {b['id']} ({b['injection_dt']}; v1's blank unknown, "
                             f"a genuine blank within {self.window.days} days)")
                   for b in near[:self.max_blanks]])

    def _prove(self, row, sample: dict, rev: Optional[dict], v1_state: _V1Blank,
               present: frozenset):
        """(tag, detail) when a recomputation reproduces v1's result columns;
        (None, why not) otherwise."""
        if self.v1_corr is None:
            return None, ("differing numbers can only be explained by recomputing with v1's "
                          "corrections file: pass --v1-corrections")
        cdf = self._cdf(sample, rev)
        if not cdf.is_file():
            return None, f"the recorded CDF is missing ({cdf})"
        cals = self._calibration_confs(rev)
        hub_blank = None
        hub_corr: dict = {}
        if rev is not None:
            hub_blank = rev.get("blank_used")
            hub_corr = dict((json.loads(rev.get("corrections_used") or "null") or {}).get("values") or {})
            own = next((c for c in cals if not c[2]), None)
            if own is None:
                return None, "the revision records no calibration anchors; nothing can be proven"
            control = self._recompute(cdf, own[1], hub_blank, hub_corr, False)
            hub = _rendered(dict(json.loads(rev["results"]), **{"Source File": ""}))
            # (a revision stored before v7.0.0 may dip; recomputing holds it)
            if control is None or any(control[c] != v7_d86(hub)[c] for c in RESULT_COLUMNS):
                return None, ("recomputing with the revision's own inputs doesn't reproduce it "
                              "(calibration, blank or CDF changed?); nothing can be proven")
        target = {c: v for c, v in v7_d86({c: row.values.get(c, "") for c in RESULT_COLUMNS
                                            if c in present}).items() if v != ""}
        blanks = self._blank_candidates(sample, v1_state)
        tried = 0
        for cal_label, conf, allow_auto in cals:
            for blank_id, blank_desc in blanks:
                tried += 1
                got = self._recompute(cdf, conf, blank_id, self.v1_corr, allow_auto)
                if got is None or any(got[c] != v for c, v in target.items()):
                    continue
                how = f"reproduced by recomputing with {blank_desc}, {cal_label} and v1's corrections"
                if allow_auto:
                    return "auto-detect-off", how + " (v1 auto-detected the calibration peaks)"
                if blank_id != hub_blank:
                    if pipeline.is_blank_name(sample["lab_id"]):
                        how += "; the hub never blank-subtracts a blank-named sample"
                    else:
                        hub_desc = f"blank sample {hub_blank}" if hub_blank is not None else "no blank"
                        how += f"; the hub used {hub_desc}"
                    return "blank-rule", how
                if any(float(self.v1_corr.get(c, 0.0)) != float(hub_corr.get(c, 0.0))
                       for c in set(self.v1_corr) | set(hub_corr)):
                    return "corrections", how + "; the hub's recorded corrections differ"
                return None, "reproduced only with the hub's own inputs (inconsistent revision)"
        return None, (f"no recomputation reproduces v1's values ({tried} tried: "
                      f"{len(blanks)} blank choice(s) x {len(cals)} calibration(s), "
                      f"v1's corrections)")

    # ── summary ────────────────────────────────────────────────────────────
    def summary(self) -> dict:
        tags: dict = {}
        by_row: dict = {}
        for d in self.differences:
            t = tags.setdefault(d["tag"], {"differences": 0, "rows": set()})
            t["differences"] += 1
            key = d["sample_id"] if d["sample_id"] != "" else ("v1", d["v1_line"])
            t["rows"].add(key)
            by_row.setdefault(key, set()).add(d["tag"])
        ordered = {t: {"differences": tags[t]["differences"], "rows": len(tags[t]["rows"])}
                   for t in TAGS if t in tags}
        d2887 = sorted(n for n in self.method_names if self._is_d2887(n))
        biggest: dict = {}
        for d in self.differences:
            if d["tag"] in PROVEN_TAGS and d["column"] in NUMERIC_COLUMNS:
                try:
                    delta = abs(float(d["v1"]) - float(d["hub"]))
                except ValueError:
                    continue
                biggest[d["tag"]] = max(biggest.get(d["tag"], 0.0), delta)
        kinds: dict = {}
        for i in self.issues:
            kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        fail = [f"{ordered[t]['rows']} {t}" for t in FAILING_TAGS if t in ordered]
        if self.verified == 0:
            fail.append("no rows were numerically verified")
        if d2887:
            fail.append(f"D2887 method name(s) excluded from processing: {', '.join(d2887)}")
        unaccepted = sorted(n for n in self.method_names
                            if n not in d2887 and (n == "(none)" or n not in self.accepted))
        if unaccepted:
            fail.append(f"excluded method name(s) not accepted: {', '.join(unaccepted)}")
        failing_keys = {k for k, v in by_row.items() if v & set(FAILING_TAGS)}
        matching = sum(1 for sid in self.compared_ids if not by_row.get(sid, set()) - {"source-file"})
        out = {
            "instrument": self.inst_id,
            "scope": dict(self.scope_info),
            # A D2887 name can't be accepted as excluded (it must be mapped):
            # never listed as accepted, reported as ignored instead.
            "accepted_methods": sorted(n for n in self.accepted if not self._is_d2887(n)),
            "accept_ignored_d2887": sorted(n for n in self.accepted if self._is_d2887(n)),
            "open_conflicts": list(self.open_conflicts),
            "max_abs_difference": {t: round(v, 6) for t, v in biggest.items()},
            "v1_csv": str(self.v1_csv),
            "generated_at": store.now_iso(),
            "hub_samples": len(self.scope),
            "v1_rows": len(self.rows),
            "v1_rows_not_in_hub": self.not_in_hub,
            "v1_rows_out_of_scope": self.out_of_scope,
            "superseded_v1_rows": self.superseded,
            "compared_rows": self.compared,
            "rows_numerically_verified": self.verified,
            "rows_matching": matching,               # compared, equal apart from Source File
            "rows_failing": len(failing_keys),
            "rows_unexplained": sum(1 for v in by_row.values() if "unexplained" in v),
            "tags": ordered,
            "unexplained": ordered.get("unexplained", {}).get("differences", 0),
            "method_excluded_names": dict(sorted(self.method_names.items())),
            "method_excluded_d2887": d2887,
            "csv_issues": kinds,
            "fail_reasons": fail,
            "verdict": "FAIL" if fail else "PASS",
        }
        out["verdict_line"] = verdict_line(out)
        return out


def parity_report(instrument_id: str, v1_results_csv, *, db: store.Db, out_dir, data_dir=None,
                  conf: Optional[dict] = None,
                  v1_corrections: Union[None, str, os.PathLike, dict] = None,
                  sample_ids: Optional[Iterable[int]] = None, since=None,
                  blank_window_days: float = 7, max_blank_candidates: int = 6,
                  accept_excluded_methods: Iterable[str] = ()) -> dict:
    """Compare the hub's samples with v1's CSV (see the module docstring)."""
    v1_corr = load_v1_corrections(v1_corrections)
    if data_dir is None:
        data_dir = Path(db).parent if not hasattr(db, "execute") else None
        if data_dir is None:
            import paths
            data_dir = paths.data_dir()
    rep = _Report(instrument_id, v1_results_csv, db=db, data_dir=data_dir, conf=conf,
                  v1_corr=v1_corr, sample_ids=sample_ids, since=since,
                  blank_window_days=blank_window_days,
                  max_blank_candidates=max_blank_candidates,
                  accepted=accept_excluded_methods).run()
    summary = rep.summary()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    csv_path = out / f"parity-{instrument_id}-{stamp}.csv"
    html_path = out / f"parity-{instrument_id}-{stamp}.html"
    _write_csv(csv_path, rep.differences)
    html_path.write_text(_render_html(summary, rep.differences), encoding="utf-8")
    return {"summary": summary, "differences": rep.differences, "csv": str(csv_path),
            "html": str(html_path), "exit_code": 1 if summary["fail_reasons"] else 0}


def _write_csv(path: Path, differences: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        w.writeheader()
        for d in differences:
            w.writerow({k: d.get(k, "") for k in OUT_COLUMNS})


HTML_LIMIT = 5000


def verdict_line(summary: dict) -> str:
    head = ("PASS: every difference is explained" if summary["verdict"] == "PASS"
            else "FAIL: " + "; ".join(summary["fail_reasons"]))
    line = (f"{head}. rows numerically verified: {summary['rows_numerically_verified']}; "
            f"v1 rows outside the scope: {summary['v1_rows_out_of_scope']}")
    if summary.get("accepted_methods"):
        line += f"; accepted excluded methods: {', '.join(summary['accepted_methods'])}"
    if summary.get("accept_ignored_d2887"):
        line += ("; not accepted (D2887 names must be mapped, not excluded): "
                 f"{', '.join(summary['accept_ignored_d2887'])}")
    return line


def _render_html(summary: dict, differences: list) -> str:
    e = html.escape
    ok = summary["verdict"] == "PASS"
    big = summary.get("max_abs_difference", {})
    tag_rows = "".join(
        f"<tr><td class='tag {'bad' if t in FAILING_TAGS else ''}'>{e(t)}</td>"
        f"<td>{'fails' if t in FAILING_TAGS else 'info'}</td><td class='n'>{c['differences']}</td>"
        f"<td class='n'>{c['rows']}</td>"
        f"<td class='n'>{'' if t not in big else f'{big[t]:.2f}'}</td><td>{e(TAG_HELP[t])}</td></tr>"
        for t, c in summary["tags"].items())
    conflicts = "".join(
        f"<tr><td class='n'>{c['id']}</td><td>{e(c['lab_id'])}</td><td>{e(c['injection_dt'])}</td>"
        f"<td class='n'>{c['existing_sample_id']}</td><td>{e(c['cdf_path'] or '')}</td></tr>"
        for c in summary.get("open_conflicts", [])) or "<tr><td colspan='5'>none</td></tr>"
    sc = summary.get("scope", {})
    scope_text = ", ".join(
        [f"sample ids ({sc['sample_ids']})"] * (sc.get("sample_ids") is not None)
        + [f"received since {sc['since']}"] * (sc.get("since") is not None)) or "every hub sample"
    methods = "".join(
        f"<tr><td>{e(n)}</td><td class='n'>{c}</td>"
        f"<td class='{'bad' if n in summary['method_excluded_d2887'] else ''}'>"
        f"{'D2887 name: FAILS' if n in summary['method_excluded_d2887'] else ('accepted' if n in summary.get('accepted_methods', []) else 'not accepted: FAILS')}</td></tr>"
        for n, c in summary["method_excluded_names"].items()) or "<tr><td colspan='3'>none</td></tr>"
    shown = [d for d in differences if d["tag"] not in ("source-file", "not-in-hub")]
    listed = "".join(
        f"<tr><td class='n'>{e(str(d['v1_line']))}</td><td>{e(d['lab_id'])}</td>"
        f"<td>{e(d['v1_injection_dt'])}</td><td class='n'>{e(str(d['sample_id']))}</td>"
        f"<td>{e(d['column'])}</td><td>{e(d['v1'])}</td><td>{e(d['hub'])}</td>"
        f"<td class='tag {'bad' if d['tag'] in FAILING_TAGS else ''}'>{e(d['tag'])}</td>"
        f"<td>{e(d['detail'])}</td></tr>"
        for d in shown[:HTML_LIMIT])
    more = (f"<p>Showing the first {HTML_LIMIT} of {len(shown)}; the CSV has them all.</p>"
            if len(shown) > HTML_LIMIT else "")
    issues = ", ".join(f"{k}: {v}" for k, v in summary["csv_issues"].items()) or "none"
    facts = [("Instrument", summary["instrument"]), ("v1 CSV", summary["v1_csv"]),
             ("Generated", summary["generated_at"]),
             ("Scope", scope_text),
             ("Hub samples in scope", summary["hub_samples"]),
             ("Compared with a v1 row", summary["compared_rows"]),
             ("Rows numerically verified", summary["rows_numerically_verified"]),
             ("Rows failing", summary["rows_failing"]),
             ("v1 rows", summary["v1_rows"]),
             ("v1 rows not in the hub (info)", summary["v1_rows_not_in_hub"]),
             ("v1 rows outside the scope", summary["v1_rows_out_of_scope"]),
             ("Superseded v1 rows", summary["superseded_v1_rows"]),
             ("Accepted excluded methods", ", ".join(summary.get("accepted_methods", [])) or "none"),
             ("CSV reader notes", issues)]
    fact_rows = "".join(f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in facts)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Parity report {e(summary['instrument'])}</title>
<style>
:root {{ --bg:#fff; --fg:#1b1f24; --muted:#5b6470; --line:#d8dde3; --ok:#1a7f37; --bad:#c62828; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#15181c; --fg:#e6e9ed; --muted:#9aa4b0;
  --line:#2c323a; --ok:#4cc26d; --bad:#ff6b6b; }} }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; margin:0 16px 32px; }}
h1 {{ font-size:20px; margin:20px 0 4px; }} h2 {{ font-size:16px; margin:24px 0 8px; }}
.verdict {{ font-weight:600; color:var(--ok); }} .verdict.bad {{ color:var(--bad); }}
.wrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; }} th, td {{ border-bottom:1px solid var(--line); padding:4px 10px;
  text-align:left; vertical-align:top; }}
th {{ color:var(--muted); font-weight:500; }} td.n {{ text-align:right; font-variant-numeric:tabular-nums; }}
td.tag {{ white-space:nowrap; font-weight:600; }} .bad {{ color:var(--bad); }}
</style></head><body>
<h1>v1 parity report: {e(summary['instrument'])}</h1>
<p class="verdict{'' if ok else ' bad'}">{e(verdict_line(summary))}</p>
<div class="wrap"><table>{fact_rows}</table></div>
<h2>Differences by tag</h2>
<div class="wrap"><table><tr><th>Tag</th><th>Gate</th><th>Differences</th><th>Rows</th>
<th>Largest difference (°C)</th><th>Meaning</th></tr>
{tag_rows}</table></div>
<h2>Methods not processed</h2>
<div class="wrap"><table><tr><th>Method</th><th>Samples</th><th></th></tr>{methods}</table></div>
<h2>Open conflicts on this instrument</h2>
<div class="wrap"><table><tr><th>Conflict</th><th>Lab ID</th><th>Injection</th><th>Existing sample</th>
<th>Held file</th></tr>{conflicts}</table></div>
<h2>Differences (Source File and not-in-hub omitted)</h2>{more}
<div class="wrap"><table><tr><th>v1 line</th><th>Lab ID</th><th>v1 InjectionDateTime</th><th>Sample</th>
<th>Column</th><th>v1</th><th>Hub</th><th>Tag</th><th>Detail</th></tr>
{listed}</table></div>
</body></html>
"""


def format_summary(summary: dict) -> str:
    lines = [verdict_line(summary),
             f"{summary['instrument']}: {summary['hub_samples']} hub sample(s) in scope, "
             f"{summary['compared_rows']} compared with v1; {summary['v1_rows']} v1 row(s) "
             f"({summary['v1_rows_not_in_hub']} not in the hub, {summary['superseded_v1_rows']} "
             f"superseded)"]
    for tag, c in summary["tags"].items():
        gate = "FAIL" if tag in FAILING_TAGS else "info"
        lines.append(f"  [{gate}] {tag}: {c['differences']} difference(s) in {c['rows']} row(s)")
    if summary["method_excluded_names"]:
        lines.append("  methods not processed: " + ", ".join(
            f"{n} ({c})" for n, c in summary["method_excluded_names"].items()))
    if summary.get("open_conflicts"):
        lines.append(f"  open conflicts on this instrument: {len(summary['open_conflicts'])}")
    lines.append(f"unexplained: {summary['unexplained']}")
    return "\n".join(lines)


def _parse_ids(text: str) -> list:
    if text.startswith("@"):
        raw = Path(text[1:]).read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return [int(i) for i in data.get("sample_ids", [])]
        if isinstance(data, list):
            return [int(i) for i in data]
        text = raw
    return [int(t) for t in re.split(r"[\s,]+", text.strip()) if t]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare the hub's results with v1's results CSV.")
    ap.add_argument("instrument", help="instrument id, e.g. gc1")
    ap.add_argument("v1_csv", type=Path, help="v1's distill_results.csv (read only)")
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="the hub's data folder (default: GC_DATA_DIR)")
    ap.add_argument("--out-dir", type=Path, default=None, help="default: <data>/reports")
    ap.add_argument("--v1-corrections", type=Path, default=None,
                    help="v1's correction_factors.json (needed to prove any numeric difference)")
    ap.add_argument("--sample-ids", default=None,
                    help="scope: 1,2,3 or @file (a loader --json summary, or ids)")
    ap.add_argument("--since", default=None, help="scope: hub samples received at or after this time")
    ap.add_argument("--accept-excluded-method", action="append", default=[], metavar="NAME",
                    help="a method name the hub doesn't process that is known not to be D2887 "
                         "(repeatable; compared like the hub compares names)")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)
    data_dir = args.data_dir or (Path(os.environ["GC_DATA_DIR"]) if os.environ.get("GC_DATA_DIR") else None)
    if data_dir is None or not (data_dir / store.DB_FILENAME).is_file():
        print(f"error: no hub database in {data_dir}", file=sys.stderr)
        return 2
    os.environ["GC_DATA_DIR"] = str(data_dir)
    if not args.v1_csv.is_file():
        print(f"error: no such CSV: {args.v1_csv}", file=sys.stderr)
        return 2
    v1_corr = None
    if args.v1_corrections is not None:
        if not args.v1_corrections.is_file():
            print(f"error: no such corrections file: {args.v1_corrections}", file=sys.stderr)
            return 2
        try:
            v1_corr = load_v1_corrections(args.v1_corrections)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        ids = _parse_ids(args.sample_ids) if args.sample_ids else None
    except (OSError, ValueError) as exc:
        print(f"error: --sample-ids: {exc}", file=sys.stderr)
        return 2
    import settings
    rep = parity_report(args.instrument, args.v1_csv, db=data_dir / store.DB_FILENAME,
                        out_dir=args.out_dir or data_dir / "reports", data_dir=data_dir,
                        conf=settings.load_settings(), v1_corrections=v1_corr, sample_ids=ids,
                        since=args.since, accept_excluded_methods=args.accept_excluded_method)
    print(json.dumps(rep["summary"], indent=2) if args.json else format_summary(rep["summary"]))
    print(f"CSV:  {rep['csv']}\nHTML: {rep['html']}")
    return rep["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
