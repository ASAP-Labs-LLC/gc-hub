#!/usr/bin/env python3
"""The v1 parity report (phase 2, 2A1 T6): the 2A1 acceptance gate.

For one instrument, every row of v1's results CSV is compared with the hub's
current revision of the same sample, column by column (all 31
``CSV_HEADER`` columns, as exact strings: the hub side is the line
``exports.format_line`` renders, which is what the hub exports). Each
difference is tagged with its explanation:

``source-file``         expected: the hub records its own relative ``cdf_path``,
                        v1 the absolute processed-file path.
``injection-time-fix``  v1 misparsed the ANDI stamp (fromisoformat first), or
                        wrote an unrounded file time; the row's
                        ``InjectionDateTime`` equals the sample's
                        ``legacy_injection_dt``.
``blank-rule``          the blank differs: the hub takes the latest genuine
                        blank injected at or before the sample, v1 its newest
                        *registered* one (worked out by replaying the CSV in
                        order: a v1 blank name, genuine per the hub's
                        ``is_blank``, replaces an older registered blank); the
                        hub never blank-subtracts a blank-named sample, which v1
                        did for ``(Blank)``/``[b] Blank``; or the hub's blank was
                        rejected for this sample and it was computed without one.
                        When v1's blank can't be determined (no blank row before
                        it, or a blank row whose CDF isn't in the hub), numbers
                        that differ are put down to the blank only when the hub
                        subtracted one.
``auto-detect-off``     the hub holds the sample ``awaiting_calibration``; v1
                        would have auto-detected the calibration peaks.
``corrections``         only D86 cuts differ, and v1's value is exactly the hub's
                        uncorrected D86 plus v1's corrections file, while the
                        hub's recorded correction for that cut differs.
``method-excluded``     the hub doesn't process the CDF's method
                        (``other_method``) or it has none (``review_method``).
``not-in-hub``          no hub sample has this lab ID and injection time (correct
                        or v1 form): its CDF wasn't loaded.
``not-processed``       the hub sample has no result yet (queued, pending
                        corrections...): run the report once the queue is empty.
``unexplained``         none of the above.

When v1 wrote several rows for one sample (reprocess, Export to LIMS), only
the last is compared (it is v1's current result); the earlier ones are
counted as ``superseded_v1_rows`` but still replayed for v1's blank.

::

    parity_report(instrument_id, v1_results_csv, *, db, out_dir,
                  v1_corrections=None) -> {"summary", "differences", "csv", "html", "exit_code"}

``v1_corrections`` is v1's ``correction_factors_json`` (a path, read as v1
read it, or a ``{cut: value}`` dict); without it a D86-only difference can't
be checked and is ``unexplained``. The report is written to ``out_dir`` as
``parity-<instrument>-<stamp>.csv`` (every difference) and ``.html`` (a
self-contained summary). ``exit_code`` is 1 when anything is
``unexplained`` or ``not-processed``, else 0.

CLI::

    python tools/parity_report.py --data-dir C:\\ASAPApps\\gc\\data \\
        --v1-corrections "\\\\asapserver\\...\\correction_factors.json" gc1 D:\\robocopy\\distill_results.csv

Exit codes: 0 every difference explained; 1 some unexplained or unprocessed;
2 bad arguments (missing CSV, data folder or database).
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
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import distill  # noqa: E402
import exports  # noqa: E402
import import_match  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402

TAGS = ("source-file", "injection-time-fix", "blank-rule", "auto-detect-off", "corrections",
        "method-excluded", "not-in-hub", "not-processed", "unexplained")
FAILING = ("unexplained", "not-processed")
TAG_HELP = {
    "source-file": "Expected: the hub records its own relative CDF path, v1 the absolute processed path.",
    "injection-time-fix": "v1 misparsed the injection stamp (or wrote an unrounded file time); "
                          "the hub stores the correct time.",
    "blank-rule": "The blank differs: at-or-before instead of v1's newest registered blank; "
                  "(Blank)/[b] Blank samples are never blank-subtracted; a rejected blank means none.",
    "auto-detect-off": "The instrument's calibration isn't usable; v1 would have auto-detected "
                       "peaks, the hub waits for a calibration.",
    "corrections": "The hub's correction value for this D86 cut differs from v1's corrections file.",
    "method-excluded": "The CDF's method isn't mapped to a hub method (or has none): not processed.",
    "not-in-hub": "No hub sample has this lab ID and injection time: its CDF wasn't loaded.",
    "not-processed": "The hub sample has no result yet: run the report when the queue is empty.",
    "unexplained": "Nothing above explains it.",
}
OUT_COLUMNS = ("v1_line", "lab_id", "v1_injection_dt", "sample_id", "hub_status", "column", "v1",
               "hub", "tag", "detail")
RESULT_COLUMNS = tuple(c for c in distill.CSV_HEADER
                       if c.startswith(("2887 ", "D86 ")) or c in ("Best Fit", "Fit Score"))
_D86_CUT = {"D86 IBP": "IBP", "D86 FBP": "FBP"}
_D86_CUT.update({f"D86 T{n}": f"{n}%" for n in (5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95)})

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


def _load_v1_corrections(v1_corrections) -> Optional[dict]:
    if v1_corrections is None:
        return None
    if isinstance(v1_corrections, dict):
        return {k: float(v) for k, v in v1_corrections.items()}
    return distill.load_d86_corrections(v1_corrections)   # lenient, exactly as v1 read it


def _hub_values(sample: dict, rev: dict) -> dict:
    line = exports.format_line(rev["results"], sample["cdf_path"])
    return dict(zip(distill.CSV_HEADER, next(csv.reader(io.StringIO(line)))))


class _V1Blank:
    """v1's reference blank while the CSV is replayed in order: ``known`` (a hub
    sample id or None) or not, and the registered blank's injection time."""

    def __init__(self):
        self.known = False            # v1 may have started with a cached blank
        self.sample_id: Optional[int] = None
        self.dt: Optional[datetime] = None

    def register(self, row: import_match.CsvRow, hub: Optional[dict]) -> None:
        cand = _parse_dt(row.injection_dt_raw)
        newer = self.dt is None or cand is None or not self.dt > cand
        if hub is None:               # its CDF isn't loaded: genuine or not is unknown
            if newer:
                self.known, self.sample_id = False, None
                self.dt = cand if cand is not None else self.dt
            return
        if not hub.get("is_blank"):   # v1 refused a blank-named run with sample signal
            return
        if newer:
            self.known, self.sample_id, self.dt = True, hub["id"], cand

    def describe(self) -> str:
        return f"blank sample {self.sample_id}" if self.sample_id is not None else "no blank"


def _blank_explanation(sample: dict, rev: dict, v1: _V1Blank) -> Optional[str]:
    lab = sample["lab_id"]
    if v1_blank_name(lab):
        return None                   # neither v1 nor the hub subtracts a blank from it
    if pipeline.is_blank_name(lab):
        return (f"v1 did not treat {lab!r} as a blank name and blank-subtracted it "
                f"({v1.describe() if v1.known else 'its blank then'}); the hub never "
                f"blank-subtracts a blank-named sample")
    try:
        notes = json.loads(rev.get("notes") or "null")
    except ValueError:
        notes = None
    if isinstance(notes, dict) and notes.get("blank_rejected"):
        rej = notes["blank_rejected"]
        return (f"the hub's blank (sample {rej.get('sample_id')}) was rejected for this sample "
                f"({rej.get('reason')}); computed without a blank")
    hub_blank = rev.get("blank_used")
    hub_desc = f"blank sample {hub_blank}" if hub_blank is not None else "no blank"
    if v1.known:
        if v1.sample_id != hub_blank:
            return (f"v1 used its newest registered blank ({v1.describe()}); the hub used "
                    f"{hub_desc} (the latest genuine blank at or before the injection)")
        return None
    if hub_blank is not None:
        return f"v1's blank can't be determined from the CSV; the hub subtracted {hub_desc}"
    return None


def _corrections_check(col: str, v1_value: str, rev: dict, v1_corr: Optional[dict]):
    """(tag, detail) for a D86 cut that differs while every 2887 value agrees."""
    cut = _D86_CUT.get(col)
    if v1_corr is None:
        return "unexplained", ("only D86 differs; pass v1's corrections file (--v1-corrections) "
                               "to check the corrections")
    try:
        uncorrected = json.loads(rev.get("d86_uncorrected") or "null") or {}
        used = (json.loads(rev.get("corrections_used") or "null") or {}).get("values") or {}
    except (ValueError, AttributeError):
        return "unexplained", "the revision's recorded D86 or corrections can't be read"
    if cut not in uncorrected:
        return "unexplained", f"the revision has no uncorrected D86 {cut}"
    v1_c = float(v1_corr.get(cut, 0.0))
    hub_c = float(used.get(cut, 0.0))
    expected = str(distill._round2(float(uncorrected[cut]) + v1_c))
    if expected == v1_value and hub_c != v1_c:
        return "corrections", f"{cut}: hub correction {hub_c:+g}, v1's file {v1_c:+g}"
    return "unexplained", (f"{cut}: v1's file gives {expected}; hub correction {hub_c:+g}, "
                           f"v1's {v1_c:+g}")


def _compare(row, sample: dict, rev: dict, v1: _V1Blank, v1_corr) -> list:
    """[(column, v1, hub, tag, detail)] for one v1 row and its hub revision."""
    hub = _hub_values(sample, rev)
    out, result_diffs = [], []
    for col in distill.CSV_HEADER:
        a, b = row.values.get(col, ""), hub.get(col, "")
        if a == b:
            continue
        if col == "Source File":
            out.append((col, a, b, "source-file", "hub-relative path vs v1's absolute path"))
        elif col == "InjectionDateTime":
            if a == (sample.get("legacy_injection_dt") or "") and a != sample["injection_dt"]:
                why = ("v1 wrote the unrounded file time" if sample["injection_dt_source"] == "mtime"
                       else "v1 misparsed the injection stamp")
                out.append((col, a, b, "injection-time-fix", why))
            else:
                out.append((col, a, b, "unexplained", "injection time differs"))
        elif col in RESULT_COLUMNS:
            result_diffs.append((col, a, b))
        else:
            out.append((col, a, b, "unexplained", f"{col} differs"))
    if not result_diffs:
        return out
    blank = _blank_explanation(sample, rev, v1)
    if blank is not None:
        out.extend((c, a, b, "blank-rule", blank) for c, a, b in result_diffs)
    elif all(c in _D86_CUT for c, _a, _b in result_diffs):
        for c, a, b in result_diffs:
            tag, detail = _corrections_check(c, a, rev, v1_corr)
            out.append((c, a, b, tag, detail))
    else:
        out.extend((c, a, b, "unexplained", "the blank, corrections and calibration agree")
                   for c, a, b in result_diffs)
    return out


def _no_result(sample: dict) -> tuple:
    status = sample["status"]
    if status in ("other_method", "review_method"):
        return "method-excluded", f"hub status {status} (method {sample.get('method_name') or 'none'})"
    if status == "awaiting_calibration":
        return "auto-detect-off", f"hub status awaiting_calibration: {sample.get('error') or ''}".strip()
    if status == "error":
        return "unexplained", f"hub status error: {sample.get('error') or ''}".strip()
    return "not-processed", f"hub status {status}"


def parity_report(instrument_id: str, v1_results_csv, *, db: store.Db, out_dir,
                  v1_corrections: Union[None, str, os.PathLike, dict] = None) -> dict:
    """Compare v1's CSV with the hub's current revisions (see the module docstring)."""
    rows, issues = import_match.read_results_csv_ex(v1_results_csv)
    v1_corr = _load_v1_corrections(v1_corrections)
    last = {}
    for i, r in enumerate(rows):
        last[(r.lab_id, r.injection_dt_raw)] = i
    differences: list = []
    compared = superseded = 0
    v1 = _V1Blank()
    with store.connection(db) as conn:
        for i, r in enumerate(rows):
            matches = store.samples.find_by_legacy(instrument_id, r.lab_id, r.injection_dt_raw, db=conn)
            sample = matches[0] if matches else None
            current = last[(r.lab_id, r.injection_dt_raw)] == i
            if current:
                compared += 1
                base = {"v1_line": r.line_no, "lab_id": r.lab_id, "v1_injection_dt": r.injection_dt_raw,
                        "sample_id": sample["id"] if sample else "",
                        "hub_status": sample["status"] if sample else ""}
                if sample is None:
                    found = [("", "", "", "not-in-hub",
                              "no hub sample with this lab ID and injection time (correct or v1 form)")]
                else:
                    rev = (store.get_revision(sample["id"], db=conn)
                           if sample["current_revision"] is not None else None)
                    if rev is None:
                        tag, detail = _no_result(sample)
                        found = [("*", "", "", tag, detail)]
                    else:
                        found = _compare(r, sample, rev, v1, v1_corr)
                    if len(matches) > 1:
                        found = [(c, a, b, t, f"{d}; also matches sample(s) "
                                  f"{', '.join(str(m['id']) for m in matches[1:])}")
                                 for c, a, b, t, d in found]
                for col, a, b, tag, detail in found:
                    differences.append(dict(base, column=col, v1=a, hub=b, tag=tag, detail=detail))
            else:
                superseded += 1
            if v1_blank_name(r.lab_id):
                v1.register(r, sample)

    summary = _summarise(instrument_id, v1_results_csv, rows, issues, compared, superseded,
                         differences)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = out / f"parity-{instrument_id}-{stamp}.csv"
    html_path = out / f"parity-{instrument_id}-{stamp}.html"
    _write_csv(csv_path, differences)
    html_path.write_text(_render_html(summary, differences), encoding="utf-8")
    exit_code = 1 if any(summary["tags"].get(t) for t in FAILING) else 0
    return {"summary": summary, "differences": differences, "csv": str(csv_path),
            "html": str(html_path), "exit_code": exit_code}


def _summarise(instrument_id, csv_path, rows, issues, compared, superseded, differences) -> dict:
    tags: dict = {}
    by_row: dict = {}
    for d in differences:
        t = tags.setdefault(d["tag"], {"differences": 0, "rows": set()})
        t["differences"] += 1
        t["rows"].add(d["v1_line"])
        by_row.setdefault(d["v1_line"], set()).add(d["tag"])
    ordered = {t: {"differences": tags[t]["differences"], "rows": len(tags[t]["rows"])}
               for t in TAGS if t in tags}
    kinds: dict = {}
    for i in issues:
        kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
    return {
        "instrument": instrument_id,
        "v1_csv": str(csv_path),
        "generated_at": store.now_iso(),
        "v1_rows": len(rows),
        "superseded_v1_rows": superseded,
        "compared_rows": compared,
        "rows_matching": compared - sum(1 for s in by_row.values() if s - {"source-file"}),
        "rows_unexplained": sum(1 for s in by_row.values() if s & set(FAILING)),
        "tags": ordered,
        "unexplained": ordered.get("unexplained", {}).get("differences", 0),
        "csv_issues": kinds,
    }


def _write_csv(path: Path, differences: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        w.writeheader()
        for d in differences:
            w.writerow({k: d.get(k, "") for k in OUT_COLUMNS})


HTML_LIMIT = 5000


def _render_html(summary: dict, differences: list) -> str:
    e = html.escape
    ok = not any(summary["tags"].get(t) for t in FAILING)
    tag_rows = "".join(
        f"<tr><td class='tag t-{e(t)}'>{e(t)}</td><td class='n'>{c['differences']}</td>"
        f"<td class='n'>{c['rows']}</td><td>{e(TAG_HELP[t])}</td></tr>"
        for t, c in summary["tags"].items())
    shown = [d for d in differences if d["tag"] != "source-file"]
    listed = "".join(
        f"<tr><td class='n'>{e(str(d['v1_line']))}</td><td>{e(d['lab_id'])}</td>"
        f"<td>{e(d['v1_injection_dt'])}</td><td class='n'>{e(str(d['sample_id']))}</td>"
        f"<td>{e(d['column'])}</td><td>{e(d['v1'])}</td><td>{e(d['hub'])}</td>"
        f"<td class='tag t-{e(d['tag'])}'>{e(d['tag'])}</td><td>{e(d['detail'])}</td></tr>"
        for d in shown[:HTML_LIMIT])
    more = (f"<p>Showing the first {HTML_LIMIT} of {len(shown)}; the CSV has them all.</p>"
            if len(shown) > HTML_LIMIT else "")
    issues = ", ".join(f"{k}: {v}" for k, v in summary["csv_issues"].items()) or "none"
    facts = [("Instrument", summary["instrument"]), ("v1 CSV", summary["v1_csv"]),
             ("Generated", summary["generated_at"]), ("v1 rows", summary["v1_rows"]),
             ("Compared (v1's current rows)", summary["compared_rows"]),
             ("Superseded v1 rows", summary["superseded_v1_rows"]),
             ("Rows matching (apart from Source File)", summary["rows_matching"]),
             ("Rows unexplained or unprocessed", summary["rows_unexplained"]),
             ("CSV reader notes", issues)]
    fact_rows = "".join(f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in facts)
    verdict = ("PASS: every difference is explained" if ok
               else f"FAIL: {summary['unexplained']} unexplained difference(s)"
               + (f", {summary['tags']['not-processed']['rows']} unprocessed sample(s)"
                  if summary["tags"].get("not-processed") else ""))
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
td.tag {{ white-space:nowrap; font-weight:600; }} .t-unexplained, .t-not-processed {{ color:var(--bad); }}
</style></head><body>
<h1>v1 parity report: {e(summary['instrument'])}</h1>
<p class="verdict{'' if ok else ' bad'}">{e(verdict)}</p>
<div class="wrap"><table>{fact_rows}</table></div>
<h2>Differences by tag</h2>
<div class="wrap"><table><tr><th>Tag</th><th>Differences</th><th>Rows</th><th>Meaning</th></tr>
{tag_rows}</table></div>
<h2>Differences (Source File omitted)</h2>{more}
<div class="wrap"><table><tr><th>v1 line</th><th>Lab ID</th><th>v1 InjectionDateTime</th><th>Sample</th>
<th>Column</th><th>v1</th><th>Hub</th><th>Tag</th><th>Detail</th></tr>
{listed}</table></div>
</body></html>
"""


def format_summary(summary: dict) -> str:
    lines = [f"{summary['instrument']}: {summary['v1_rows']} v1 row(s), {summary['compared_rows']} "
             f"compared ({summary['superseded_v1_rows']} superseded), {summary['rows_matching']} "
             f"matching apart from Source File"]
    for tag, c in summary["tags"].items():
        lines.append(f"  {tag}: {c['differences']} difference(s) in {c['rows']} row(s)")
    lines.append(f"unexplained: {summary['unexplained']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare v1's results CSV with the hub's results.")
    ap.add_argument("instrument", help="instrument id, e.g. gc1")
    ap.add_argument("v1_csv", type=Path, help="v1's distill_results.csv (read only)")
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="the hub's data folder (default: GC_DATA_DIR)")
    ap.add_argument("--out-dir", type=Path, default=None, help="default: <data>/reports")
    ap.add_argument("--v1-corrections", default=None,
                    help="v1's correction_factors.json (to check D86-only differences)")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)
    data_dir = args.data_dir or (Path(os.environ["GC_DATA_DIR"]) if os.environ.get("GC_DATA_DIR") else None)
    if data_dir is None or not (data_dir / store.DB_FILENAME).is_file():
        print(f"error: no hub database in {data_dir}", file=sys.stderr)
        return 2
    if not args.v1_csv.is_file():
        print(f"error: no such CSV: {args.v1_csv}", file=sys.stderr)
        return 2
    rep = parity_report(args.instrument, args.v1_csv, db=data_dir / store.DB_FILENAME,
                        out_dir=args.out_dir or data_dir / "reports",
                        v1_corrections=args.v1_corrections)
    print(json.dumps(rep["summary"], indent=2) if args.json else format_summary(rep["summary"]))
    print(f"CSV:  {rep['csv']}\nHTML: {rep['html']}")
    return rep["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
