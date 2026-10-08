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
# (or explains everything away) fails: (config, case) -> verdict. Since
# v7.0.0 most synthetic samples are ``d86-monotonic``: the fixtures' narrow
# envelopes give an uncorrected D86 T10 below T5 (and the reference factors
# lower T10 by 5.25), which v1 reported as it fell and the hub holds.
PINNED = {
    ('operator', 'noisy'): 'match',
    ('operator', 'saturating'): 'd86-monotonic',
    ('operator', 'near_zero'): 'd86-monotonic',
    ('operator', 'weak_sample_under_bleed'): 'd86-monotonic',
    ('operator', 'csv_quoting_name'): 'd86-monotonic',
    ('operator', 'stamp_25_00'): 'd86-monotonic+injection-time-fix',
    ('operator', 'stamp_25_13'): 'd86-monotonic',
    ('operator', 'no_stamp_mtime'): 'd86-monotonic+injection-time-fix',
    ('operator', 'no_stamp_mtime_late_fraction'): 'd86-monotonic+injection-time-fix',
    ('operator', 'injected_before_blank'): 'blank-rule',
    ('operator', 'paren_blank_named'): 'blank-rule',
    ('operator', 'no_sample_name'): 'd86-monotonic+lab-id-fallback',
    ('operator', 'other_method'): 'method-excluded',
    ('operator', 'no_method'): 'review-method',
    ('operator', 'truncated_copy'): 'truncated-refused',
    ('operator', 'duplicate_bytes'): 'no-new-row',
    ('operator', 'conflict_same_key'): 'no-new-row',
    ('operator', 'nan_point'): 'both-failed',
    ('operator', 'crude_wide'): 'match',
    ('operator', 'solvent_heavy'): 'match',
    ('three', 'diesel'): 'd86-monotonic',
    ('two', 'diesel'): 'd86-monotonic',
    ('all11', 'diesel'): 'd86-monotonic',
    ('auto_zip', 'diesel'): 'd86-monotonic',
    ('none', 'diesel'): 'auto-detect-off',
    ('corr_null', 'diesel'): 'corrections-pending',
    ('corr_missing', 'diesel'): 'corrections-pending',
    ('corr_huge', 'diesel'): 'corrections-pending',
}

# Pinned per suite: the synthetic fixtures' envelopes and the real blank's
# baseline give different D86 shapes, so whether v1's corrected series dipped
# (``d86-monotonic``) differs between them.
PINNED_BY_SUITE = {
    "synthetic": {
        ('operator', 'diesel'): 'd86-monotonic',
        ('operator', 'short_run_60pct'): 'd86-monotonic',
        ('operator', 'float32'): 'd86-monotonic',
        ('operator', 'coarse_sampling_x10'): 'd86-monotonic',
        ('operator', 'misparsed_stamp'): 'd86-monotonic+injection-time-fix+late-blank-review',
        ('operator', 'before_blank2_arriving_after'): 'd86-monotonic+blank-rule',
        ('operator', 'after_blank2'): 'd86-monotonic',
        ('nofit', 'diesel'): 'd86-monotonic',
        ('misassigned', 'diesel'): 'd86-monotonic+calibration-filter',
    },
    "real": {
        ('operator', 'diesel'): 'match',
        ('operator', 'short_run_60pct'): 'match',
        ('operator', 'float32'): 'match',
        ('operator', 'coarse_sampling_x10'): 'match',
        ('operator', 'misparsed_stamp'): 'injection-time-fix+late-blank-review',
        ('operator', 'before_blank2_arriving_after'): 'blank-rule',
        ('operator', 'after_blank2'): 'match',
        ('nofit', 'diesel'): 'match',
        ('misassigned', 'diesel'): 'calibration-filter',
    },
}


def _check(findings):
    bad = rh.unexplained(findings)
    assert not bad, rh.report(findings)
    got = {(f.config, f.case): f.verdict for f in findings}
    (suite,) = {f.suite for f in findings}
    pinned = {**PINNED, **PINNED_BY_SUITE[suite]}
    wrong = {k: (v, got.get(k)) for k, v in pinned.items() if got.get(k) != v}
    assert not wrong, f"pinned verdicts (expected, got): {wrong}\n{rh.report(findings)}"
    c = rh.counts(findings)
    # Equal string for string, or equal but for the corrected D86 cells v1
    # let dip (each proven by _d86_monotonic).
    assert c.get("match", 0) + c.get("d86-monotonic", 0) >= 120, rh.report(findings)
    assert c.get("match", 0) >= 15, rh.report(findings)


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


@needs_snapshot
def test_v1_production_rows_under_the_v7_rule():
    """v7.0.0 on v1's 80 production rows: the hub's corrected D86
    (``monotonic_d86`` of the corrected conversion of v1's D2887) equals v1's
    cell everywhere v1's corrected series did not dip, and the highest earlier
    v1 cell where it did; the uncorrected conversion is untouched. 38 of the
    80 rows dipped (mostly blanks at FBP; 36394, 36395, 37516 and the April
    gas standard at T10)."""
    corr = distill.load_d86_corrections(rh.REAL_CORRECTIONS)
    changed_rows = 0
    for r in _v1_rows():
        d2887 = {k: float(r[f"2887 {c}"]) for k, c in zip(LABELS, CUTS)}
        unc = distill.x4_midpoints(distill._convert_to_d86(d2887))
        corrected = distill.apply_d86_corrections(unc, corr)
        held = distill.monotonic_d86(corrected)
        assert distill.x4_midpoints(distill._convert_to_d86(d2887)) == unc
        best, changed = None, False
        for k, c in zip(LABELS, CUTS):
            v1 = r[f"D86 {c}"]
            if best is not None and float(v1) < float(best):
                assert str(held[k]) == best, (r["Lab ID"], c)
                changed = True
            else:
                assert str(held[k]) == v1, (r["Lab ID"], c)
                best = v1
        changed_rows += changed
    assert changed_rows == 38


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


def _fragile_compare(hub_t80: str, *, fragile: bool, cross_platform: bool) -> str:
    """compare() on one sample whose only difference from v1 is D86 T80."""
    case = rh.Case("near_zero_noisy", lambda s, t: None, fp_fragile=fragile)
    item = rh.Item("near_zero_noisy", Path("near_zero_noisy.CDF"), case, "2026-09-25 14:23:00")
    v1_row = {c: "1" for c in rh.COLUMNS}
    v1_row.update({"InjectionDateTime": item.expect_dt, "D86 T80": "376.57"})
    hub_row = dict(v1_row, **{"D86 T80": hub_t80})
    hub = {item.id: {"row": hub_row, "status": "final", "blank": None, "line": None}}
    v1_rec = {"row": v1_row, "blank_id": None, "line": None, "errors": [], "rows_added": 1}
    v1 = {"looker": {item.id: v1_rec}, "direct": {item.id: v1_rec}}
    [finding] = [f for f in rh.compare("synthetic", "operator", [item], hub, v1,
                                        cross_platform=cross_platform)
                 if f.case == item.id]
    return finding.verdict


def test_a_fragile_case_may_move_a_hundredth_only_against_another_machines_recording():
    """CI (Linux x86-64) moved near_zero_noisy's cut points by up to 0.05 from
    the recording made on a Mac (arm64), same code and numpy/scipy."""
    assert _fragile_compare("376.56", fragile=True, cross_platform=True) == "fp-platform"
    # live (same machine): exact, as every other case
    assert _fragile_compare("376.56", fragile=True, cross_platform=False) == "UNEXPLAINED"
    # only cases marked fragile
    assert _fragile_compare("376.56", fragile=False, cross_platform=True) == "UNEXPLAINED"
    # never more than the tolerance
    assert _fragile_compare("376.80", fragile=True, cross_platform=True) == "UNEXPLAINED"


# ── v7.0.0 "Corrected D86 never decreases": the d86-monotonic proof ─────────

def _d86_row(values):
    row = {c: "1" for c in rh.COLUMNS}
    row.update(dict(zip(rh.D86_COLUMNS, values)))
    return row


V1_DIP = ["163.76", "186.19", "184.8", "197.6", "204.12", "212.31", "216.44", "231.94",
          "243.38", "252.25", "256.81", "267.27", "271.06"]
HUB_HELD = list(V1_DIP)
HUB_HELD[2] = "186.19"


def _monotonic_verdict(v1_values, hub_values, **extra):
    v1_row, hub_row = _d86_row(v1_values), _d86_row(hub_values)
    hub_row.update(extra)
    diff = rh._row_diff(v1_row, hub_row)
    item = rh.Item("x", Path("x.CDF"), None, "1")
    return rh._explain_columns(diff, {}, None, item, v1_row, hub_row)


def test_d86_monotonic_explains_only_a_held_dip():
    """operator/diesel: v1's corrected T10 (184.8) is below its T5 (186.19);
    the hub reports 186.19 there. That and nothing else is explained."""
    assert _monotonic_verdict(V1_DIP, HUB_HELD) == (["d86-monotonic"], {})
    # several cuts held at one value (a long dip), and FBP
    v1 = ["100", "120", "110", "115", "119", "125", "130", "131", "132", "133", "134", "140", "139"]
    hub = ["100", "120", "120", "120", "120", "125", "130", "131", "132", "133", "134", "140", "140"]
    assert _monotonic_verdict(v1, hub) == (["d86-monotonic"], {})


def test_d86_monotonic_refuses_anything_else():
    # held at the wrong value (not the earlier cut's)
    wrong = list(HUB_HELD); wrong[2] = "186.2"
    assert _monotonic_verdict(V1_DIP, wrong)[0] == []
    # a D86 change where v1 did not dip
    moved = list(HUB_HELD); moved[3] = "197.61"
    verdicts, left = _monotonic_verdict(V1_DIP, moved)
    assert verdicts == [] and set(left) == {"D86 T10", "D86 T20"}
    # the hub's series still dips somewhere v1's also did (not held everywhere)
    v1 = ["100", "120", "110", "105", "130"] + ["131"] * 8
    hub = ["100", "120", "120", "105", "130"] + ["131"] * 8
    assert _monotonic_verdict(v1, hub)[0] == []
    # a D2887 column differing alongside is never explained by it
    verdicts, left = _monotonic_verdict(V1_DIP, HUB_HELD, **{"2887 T10": "2"})
    assert verdicts == ["d86-monotonic"] and set(left) == {"2887 T10"}
    # without the full rows nothing is proven
    item = rh.Item("x", Path("x.CDF"), None, "1")
    diff = rh._row_diff(_d86_row(V1_DIP), _d86_row(HUB_HELD))
    assert rh._explain_columns(diff, {}, None, item) == ([], diff)


def test_d86_monotonic_is_documented():
    assert "d86-monotonic" in rh.DOCUMENTED
    assert "d86-monotonic" in (rh.__doc__ or "")
