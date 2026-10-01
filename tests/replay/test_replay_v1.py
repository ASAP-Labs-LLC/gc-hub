"""Differential replay: the hub's pipeline against the v1 production code
(``replay_harness.py`` says how, and which differences are deliberate).

* ``test_hub_matches_recorded_v1`` (always, CI included): the synthetic sweep
  through the hub, compared with v1's output for the same CDFs as recorded
  by ``record_v1.py`` from the share snapshot's v1 code.
* With the share snapshot present (``GC_SNAPSHOT_DIR``, or
  ``gc-share-snapshot-2026-09-25`` beside the checkout), v1 runs live:
  the synthetic sweep (and the recording must still equal it), the sweep on
  the real calibration run and blank, and the 80 rows of v1's results CSV.
  Skipped cleanly otherwise.
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("netCDF4")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_harness as rh  # noqa: E402
import distill  # noqa: E402

RECORDING = rh.REPLAY_DIR / "v1_recorded_synthetic.json"
needs_snapshot = pytest.mark.skipif(not rh.snapshot_available(),
                                    reason="the v1 share snapshot is not available")

# Cases whose verdict is pinned, so a harness that silently compares nothing
# (or explains everything away) fails: (config, case) -> verdict.
PINNED = {
    ("operator", "diesel"): "match",
    ("operator", "noisy"): "match",
    ("operator", "short_run_60pct"): "match",
    ("operator", "saturating"): "match",
    ("operator", "float32"): "match",
    ("operator", "coarse_sampling_x10"): "match",
    ("operator", "near_zero"): "match",                  # the height guard, both sides
    ("operator", "weak_sample_under_bleed"): "match",    # zero area after the blank: no blank
    ("operator", "csv_quoting_name"): "match",
    ("operator", "misparsed_stamp"): "injection-time-fix+late-blank-review",
    ("operator", "stamp_25_00"): "injection-time-fix",
    ("operator", "stamp_25_13"): "match",
    ("operator", "no_stamp_mtime"): "injection-time-fix",
    ("operator", "no_stamp_mtime_late_fraction"): "injection-time-fix",
    ("operator", "injected_before_blank"): "blank-rule",
    ("operator", "paren_blank_named"): "blank-rule",
    ("operator", "before_blank2_arriving_after"): "blank-rule",
    ("operator", "after_blank2"): "match",
    ("operator", "no_sample_name"): "lab-id-fallback",
    ("operator", "other_method"): "method-excluded",
    ("operator", "no_method"): "review-method",
    ("operator", "truncated_copy"): "truncated-refused",
    ("operator", "duplicate_bytes"): "no-new-row",
    ("operator", "conflict_same_key"): "no-new-row",
    ("operator", "nan_point"): "both-failed",
    ("three", "diesel"): "match",
    ("two", "diesel"): "match",
    ("all11", "diesel"): "match",
    ("nofit", "diesel"): "match",
    ("auto_zip", "diesel"): "match",
    ("none", "diesel"): "auto-detect-off",
    ("misassigned", "diesel"): "calibration-filter",
    ("corr_null", "diesel"): "corrections-pending",
    ("corr_missing", "diesel"): "corrections-pending",
    ("corr_huge", "diesel"): "corrections-pending",
}


def _check(findings):
    bad = rh.unexplained(findings)
    assert not bad, rh.report(findings)
    got = {(f.config, f.case): f.verdict for f in findings}
    wrong = {k: (v, got.get(k)) for k, v in PINNED.items() if got.get(k) != v}
    assert not wrong, f"pinned verdicts (expected, got): {wrong}\n{rh.report(findings)}"
    assert rh.counts(findings).get("match", 0) >= 120, rh.report(findings)


def _strip_errors(recording: dict) -> dict:
    """The recording without v1's log lines (they name temp paths)."""
    out = json.loads(json.dumps(recording))
    for cfg in out.values():
        for side in (cfg["v1"], cfg["proof"]):
            for part in (side or {}).values():
                for rec in part.values():
                    rec.pop("errors", None)
    return out


def test_hub_matches_recorded_v1(tmp_path):
    recorded = json.loads(RECORDING.read_text(encoding="utf-8"))
    findings, _raw = rh.run_suite(rh.synthetic_suite(tmp_path), tmp_path, recorded=recorded)
    _check(findings)


def test_a_wrong_hub_injection_time_is_caught(tmp_path, monkeypatch):
    """The ``injection-time-fix`` allowance must not trust the hub's own
    parse: a hub that reads compact stamps in hours 00-02 seven seconds late
    fails the replay (each case's expected time is known independently)."""
    from datetime import timedelta
    real = distill.parse_injection_datetime

    def late(raw):
        got = real(raw)
        text = (raw or "").strip()
        if got is not None and len(text) >= 14 and text[:14].isdigit() and got.hour <= 2:
            return got + timedelta(seconds=7)
        return got

    monkeypatch.setattr(distill, "parse_injection_datetime", late)
    recorded = json.loads(RECORDING.read_text(encoding="utf-8"))
    findings, _raw = rh.run_suite(rh.synthetic_suite(tmp_path), tmp_path,
                                  configs=["operator"], recorded=recorded)
    bad = {f.case for f in rh.unexplained(findings)}
    assert {"stamp_25_00", "stamp_25_01", "stamp_25_02", "misparsed_stamp"} <= bad, \
        rh.report(findings)


@needs_snapshot
def test_hub_matches_live_v1_on_synthetic_inputs(tmp_path):
    findings, raw = rh.run_suite(rh.synthetic_suite(tmp_path), tmp_path)
    _check(findings)
    recorded = json.loads(RECORDING.read_text(encoding="utf-8"))
    assert _strip_errors(rh.record(raw)) == _strip_errors(recorded), \
        "v1's output differs from the recording: re-run tests/replay/record_v1.py"


@needs_snapshot
def test_hub_matches_live_v1_on_real_inputs(tmp_path):
    """The snapshot's real calibration run (Feb24 n-alkanes) and real blank,
    the samples synthesised on the real blank's time axis and baseline, plus
    the calibration run itself processed as a sample."""
    findings, _raw = rh.run_suite(rh.real_suite(tmp_path), tmp_path)
    _check(findings)


# ── v1's own results CSV (80 real rows) ─────────────────────────────────────

LABELS = ["IBP", "5%", "10%", "20%", "30%", "40%", "50%", "60%", "70%", "80%", "90%", "95%", "FBP"]
CUTS = ["IBP", "T5", "T10", "T20", "T30", "T40", "T50", "T60", "T70", "T80", "T90", "T95", "FBP"]


def _v1_rows():
    with rh.V1_RESULTS_CSV.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@needs_snapshot
def test_hub_d86_from_v1_d2887_reproduces_every_v1_row():
    """Each of v1's 80 production rows: the hub's App. X4 conversion, 40/60
    midpoints and the reference correction factors, applied to v1's own
    D2887 cells, give v1's D86 cells string for string (as the hub writes
    them: ``str`` of a 2-dp float)."""
    rows = _v1_rows()
    assert len(rows) == 80
    corr = distill.load_d86_corrections(rh.REAL_CORRECTIONS)
    assert len(corr) == 5
    bad = []
    for r in rows:
        d2887 = {k: float(r[f"2887 {c}"]) for k, c in zip(LABELS, CUTS)}
        d86 = distill.apply_d86_corrections(distill.x4_midpoints(distill._convert_to_d86(d2887)),
                                            corr)
        for k, c in zip(LABELS, CUTS):
            if str(d86[k]) != r[f"D86 {c}"]:
                bad.append((r["Lab ID"], c, r[f"D86 {c}"], d86[k]))
    assert not bad, bad[:10]


JS_RUNNER = r"""
const fs = require('fs');
const R = require(process.argv[1] + '/static/js/results_logic.js');
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
process.stdout.write(JSON.stringify(cases.map((c) => R.convertToD86(c))));
"""


@needs_snapshot
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_results_page_uncorrected_d86_of_every_v1_row(tmp_path):
    """The Results page's Corrected D86 off (``results_logic.convertToD86``),
    on v1's own rows, equals the hub's uncorrected D86 (``distill``)."""
    cases = [{k: float(r[f"2887 {c}"]) for k, c in zip(LABELS, CUTS)} for r in _v1_rows()]
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(cases), encoding="utf-8")
    out = subprocess.run([shutil.which("node"), "-e", JS_RUNNER, str(rh.REPO_ROOT), str(f)],
                         capture_output=True, text=True, check=True, timeout=60).stdout
    bad = []
    for case, js in zip(cases, json.loads(out)):
        py = distill.x4_midpoints(distill._convert_to_d86(case))
        bad += [(k, py[k], js.get(k)) for k in LABELS if js.get(k) != py[k]]
    assert not bad, bad[:10]
