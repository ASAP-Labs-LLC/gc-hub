"""v7.0.0 "Corrected D86 never decreases" (Ryan: "don't allow a later
distillation point to go to a temperature below an earlier distillation
point, just make it the same ... But for uncorrected it can").

``distill.monotonic_d86`` holds the corrected (reported) D86 series at a
running maximum from IBP to FBP, 40 %/60 % included; ``distill.compute``
applies it after the correction factors, so the results row (the CSV LEM
reads) never dips, while ``d86_uncorrected`` and the D2887 cells keep the
plain math. The browser's mirror (``static/js/distill_view.js``
``monotonicD86``) must give the same numbers: node runs the real code on
random series (skipped without node).
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for _p in (ROOT, TESTS_DIR, TESTS_DIR / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("numpy")
import distill  # noqa: E402

NODE = shutil.which("node")
LABELS = list(distill.D86_ORDER)
D86_COLS = distill.CSV_HEADER[15:28]


# ── the rule ────────────────────────────────────────────────────────────────

def test_order_is_the_csv_order():
    assert LABELS == ["IBP", "5%", "10%", "20%", "30%", "40%", "50%", "60%", "70%", "80%",
                      "90%", "95%", "FBP"]
    assert [c.replace("D86 ", "").replace("T", "") + ("" if c.endswith(("IBP", "FBP")) else "%")
            for c in D86_COLS] == LABELS


def test_a_dip_is_held_at_the_earlier_value():
    d86 = dict(zip(LABELS, [150.0, 170.5, 165.25, 180.0, 190.0, 195.0, 200.0, 205.0, 210.0,
                            220.0, 230.0, 240.0, 235.0]))
    out = distill.monotonic_d86(d86)
    assert out["10%"] == 170.5           # T10 below T5 → T5's value
    assert out["FBP"] == 240.0           # FBP below T95 → T95's value
    assert {k: v for k, v in out.items() if k not in ("10%", "FBP")} == \
        {k: v for k, v in d86.items() if k not in ("10%", "FBP")}
    assert d86["10%"] == 165.25          # a copy: the input is untouched


def test_a_long_dip_holds_every_cut_at_the_highest_earlier_one():
    d86 = dict(zip(LABELS, [100.0, 120.0, 110.0, 115.0, 119.99, 120.0, 121.0, 118.0, 125.0,
                            130.0, 135.0, 140.0, 145.0]))
    out = distill.monotonic_d86(d86)
    assert [out[k] for k in LABELS] == [100.0, 120.0, 120.0, 120.0, 120.0, 120.0, 121.0, 121.0,
                                        125.0, 130.0, 135.0, 140.0, 145.0]
    # an IBP above everything holds the whole series
    flat = distill.monotonic_d86(dict(zip(LABELS, [500.0] + [100.0 + i for i in range(12)])))
    assert set(flat.values()) == {500.0}


def test_equal_values_and_increasing_series_are_unchanged():
    up = dict(zip(LABELS, [100.0 + 10 * i for i in range(13)]))
    assert distill.monotonic_d86(up) == up
    ties = dict(zip(LABELS, [100.0, 100.0, 100.0] + [110.0 + i for i in range(10)]))
    assert distill.monotonic_d86(ties) == ties


def test_missing_cuts_are_skipped():
    d86 = {"IBP": 150.0, "5%": 170.0, "10%": 160.0, "50%": 165.0, "FBP": ""}
    out = distill.monotonic_d86(d86)
    assert out == {"IBP": 150.0, "5%": 170.0, "10%": 170.0, "50%": 170.0, "FBP": ""}
    assert "40%" not in out
    assert distill.monotonic_d86({}) == {}


def test_midpoints_are_part_of_the_corrected_series():
    """40 %/60 % are uncorrected midpoints, but they sit in the reported
    series: a 30 % raised by its factor above the 40 % midpoint holds it."""
    unc = distill.x4_midpoints(dict(zip(
        ["IBP", "5%", "10%", "20%", "30%", "50%", "70%", "80%", "90%", "95%", "FBP"],
        [150.0, 160.0, 170.0, 180.0, 190.0, 200.0, 210.0, 220.0, 230.0, 240.0, 250.0])))
    assert unc["40%"] == 195.0
    corrected = distill.apply_d86_corrections(unc, {"30%": 8.0})
    assert corrected["30%"] == 198.0 and corrected["40%"] == 195.0
    held = distill.monotonic_d86(corrected)
    assert held["40%"] == 198.0 and held["50%"] == 200.0


# ── compute: corrected held, uncorrected and D2887 untouched ────────────────

@pytest.fixture(scope="module")
def golden_case(tmp_path_factory):
    import make_golden
    root = tmp_path_factory.mktemp("d86-monotonic")
    inputs = make_golden.build_inputs(root)
    conf = make_golden.case_conf(root, inputs, "sample_40304_blank")
    cdf, blank = make_golden.case_paths(inputs, "sample_40304_blank")
    return cdf, blank, conf


def _compute(golden_case, corrections):
    cdf, blank, conf = golden_case
    return distill.compute(cdf, conf, blank, corrections=corrections, honour_env=False)


def test_a_correction_that_makes_t10_dip_below_ibp_is_held(golden_case):
    """A synthetic factor set where the corrected T10 falls below the
    corrected IBP (and T5): the reported T10 is held at the higher of them;
    the uncorrected T10 keeps its value and D2887 is unchanged."""
    plain = _compute(golden_case, {})
    unc = plain["d86_uncorrected"]
    assert distill.monotonic_d86(unc) == unc, "the fixture itself must not dip"
    # IBP raised 4 °C above the uncorrected T5; T10 pushed 5 °C below that
    ibp = round(unc["5%"] - unc["IBP"] + 4.0, 2)
    corr = {"IBP": ibp, "10%": round((unc["IBP"] + ibp) - unc["10%"] - 5.0, 2)}
    res = _compute(golden_case, corr)
    corrected_ibp = round(unc["IBP"] + ibp, 2)
    assert corrected_ibp > unc["5%"], "the corrected IBP must lie above T5 for this case"
    assert res["d86_uncorrected"] == unc                       # the plain math, dip-free here
    assert res["d2887"] == plain["d2887"]
    assert res["d86"]["IBP"] == corrected_ibp
    assert res["d86"]["5%"] == corrected_ibp                   # T5 (uncorrected 5%) held too
    assert res["d86"]["10%"] == corrected_ibp                  # the dip: held at IBP
    raw_t10 = round(unc["10%"] + corr["10%"], 2)
    assert raw_t10 < corrected_ibp
    row = res["row"]
    assert row["D86 IBP"] == row["D86 T5"] == row["D86 T10"] == corrected_ibp
    assert [row[c] for c in distill.CSV_HEADER[2:15]] == [plain["row"][c] for c in distill.CSV_HEADER[2:15]]
    series = [row[c] for c in D86_COLS]
    assert all(b >= a for a, b in zip(series, series[1:]))
    # every cut that was not below an earlier one is exactly value + factor
    for k in LABELS:
        if k not in ("5%", "10%"):
            assert res["d86"][k] == round(unc[k] + corr.get(k, 0.0), 2), k


def test_uncorrected_keeps_a_dip(golden_case, monkeypatch):
    """When the plain App. X4 conversion itself dips, d86_uncorrected keeps
    the dip ("so we can see the true behavior of the typical math") and only
    the reported series is held."""
    real = distill._convert_to_d86

    def dipping(d2887):
        out = real(d2887)
        out["10%"] = round(out["5%"] - 3.0, 2)
        return out

    monkeypatch.setattr(distill, "_convert_to_d86", dipping)
    res = _compute(golden_case, {"IBP": -12.08, "10%": -5.25})
    unc = res["d86_uncorrected"]
    assert unc["10%"] < unc["5%"]                              # the dip, kept
    assert res["d86"]["10%"] == res["d86"]["5%"] == unc["5%"]  # reported: held at T5
    assert res["row"]["D86 T10"] == unc["5%"]


def test_no_corrections_still_reports_a_series_that_never_dips(golden_case, monkeypatch):
    real = distill._convert_to_d86
    monkeypatch.setattr(distill, "_convert_to_d86",
                        lambda d: dict(real(d), FBP=round(real(d)["95%"] - 1.0, 2)))
    res = _compute(golden_case, {})
    assert res["d86_uncorrected"]["FBP"] < res["d86_uncorrected"]["95%"]
    assert res["d86"]["FBP"] == res["d86"]["95%"]


def test_golden_rows_change_only_where_v1_dipped():
    """The v7 expectation applied to v1.0.0's golden rows (make_golden.v7_expected)
    touches D86 cells only, and only the ones below an earlier D86 cell;
    sample_40304_noblank is such a row (its corrected T10 dips)."""
    import make_golden
    golden = json.loads(make_golden.GOLDEN_JSON.read_text(encoding="utf-8"))
    changed = {}
    for name, row in golden.items():
        exp = make_golden.v7_expected(row)
        diff = {c for c in row if row[c] != exp[c]}
        assert diff <= set(D86_COLS), name
        changed[name] = sorted(diff)
        for c in diff:
            i = D86_COLS.index(c)
            assert float(row[c]) < max(float(row[p]) for p in D86_COLS[:i] if row[p] != "")
    assert changed["sample_40304_noblank"] == ["D86 T10"]


# ── the browser's mirror ────────────────────────────────────────────────────

RUNNER = r"""
const fs = require('fs');
const DV = require(process.argv[1] + '/static/js/distill_view.js');
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
process.stdout.write(JSON.stringify(cases.map((c) => ({ held: DV.monotonicD86(c), dip: DV.firstDip(c) }))));
"""


def _mirror_cases(n: int) -> list:
    rng = random.Random(20261007)
    cases = []
    for _ in range(n):
        base = rng.uniform(30, 400)
        vals = []
        for _k in LABELS:
            base += rng.uniform(-12, 25)
            vals.append(round(base, 2))
        case = dict(zip(LABELS, vals))
        for k in LABELS:                 # some cuts missing
            if rng.random() < 0.05:
                case[k] = None if rng.random() < 0.5 else ""
        cases.append(case)
    cases.append({})
    cases.append(dict(zip(LABELS, [100.0] * 13)))
    return cases


def _first_dip(case: dict):
    running = None
    for k in LABELS:
        v = case.get(k)
        if v is None or v == "":
            continue
        if running is not None and v < running:
            return k
        running = v
    return None


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_js_mirror_matches_python(tmp_path):
    cases = _mirror_cases(20000)
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(cases), encoding="utf-8")
    out = subprocess.run([NODE, "-e", RUNNER, str(ROOT), str(f)], capture_output=True,
                         text=True, check=True, timeout=120).stdout
    got = json.loads(out)
    assert len(got) == len(cases)
    bad = []
    dips = 0
    for case, js in zip(cases, got):
        py = distill.monotonic_d86({k: v for k, v in case.items() if v is not None})
        for k in LABELS:
            want = py.get(k, None)
            if js["held"].get(k, None) != want:
                bad.append((case, k, want, js["held"].get(k)))
        if js["dip"] != _first_dip(case):
            bad.append((case, "dip", _first_dip(case), js["dip"]))
        dips += js["dip"] is not None
    assert not bad, bad[:5]
    assert dips > 1000          # the random walk dips often: the hold is really exercised
