"""The Dashboard's D86 fallback for a v1 row (no stored uncorrected D86, so
"Corrected D86" off converts the stored D2887 in the browser) must give the
numbers ``distill.compute`` would: the App. X4 cuts from results_logic.js
``convertToD86`` (the Samples page's Results card; the classic page's app.js
copy went in v6.0.0) and 40%/60% by the same midpoint rule
(``distill.x4_midpoints``: the midpoint of the 30/50 and 50/70 conversions,
rounded like Python's ``round(x, 2)``). Node runs the real results_logic.js
and distill_view.js code on random D2887 rows; skipped without node.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("numpy")
import distill  # noqa: E402

NODE = shutil.which("node")
LABELS = ["IBP", "5%", "10%", "20%", "30%", "40%", "50%", "60%", "70%", "80%", "90%", "95%", "FBP"]
D2887_COLS = distill.CSV_HEADER[2:15]
D86_COLS = distill.CSV_HEADER[15:28]

# The Samples page's fallback: results_logic's convertToD86 (GCResults, the
# one App. X4 conversion in the browser since v6.0.0 removed app.js's copy)
# through DistillView.dashboardD86 (samples_logic.resultRows calls it so).
RUNNER = r"""
const fs = require('fs');
const root = process.argv[1], casesFile = process.argv[2];
global.DistillView = require(root + '/static/js/distill_view.js');
const convertToD86 = require(root + '/static/js/results_logic.js').convertToD86;
const cases = JSON.parse(fs.readFileSync(casesFile, 'utf8'));
const out = cases.map((d2887) => {
  const r = DistillView.dashboardD86({ d86: {}, d86_uncorrected: null, d2887 }, false, convertToD86);
  return { values: r.values, notes: r.notes };
});
process.stdout.write(JSON.stringify(out));
"""


def _cases(n: int) -> list:
    rng = random.Random(20260929)
    cases = []
    for _ in range(n):
        vals = sorted(round(rng.uniform(20, 600), 2) for _ in range(13))
        cases.append(dict(zip(D2887_COLS, vals)))
    # ties: 30/50 and 50/70 conversions that differ by an odd hundredth
    for base in (100.0, 187.5, 250.25, 333.33):
        for step in (0.01, 0.03, 0.05, 0.125, 0.375):
            vals = [base + i * step * 7 for i in range(13)]
            cases.append(dict(zip(D2887_COLS, [round(v, 2) for v in vals])))
    return cases


def _python(case: dict) -> dict:
    """What distill.compute stores as d86_uncorrected for this D2887 row."""
    d2887 = {label: case[col] for label, col in zip(LABELS, D2887_COLS)}
    return distill.x4_midpoints(distill._convert_to_d86(d2887))


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_dashboard_x4_fallback_matches_python(tmp_path):
    cases = _cases(60000)
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(cases), encoding="utf-8")
    out = subprocess.run([NODE, "-e", RUNNER, str(ROOT), str(f)], capture_output=True,
                         text=True, check=True, timeout=120).stdout
    got = json.loads(out)
    assert len(got) == len(cases)
    mismatches = []
    for case, js in zip(cases, got):
        py = _python(case)
        for label in LABELS:
            if js["values"][label] != py[label]:
                mismatches.append((label, py[label], js["values"][label], case))
        # 40%/60% are filled, so no "—" and no "missing" note for them
        assert "40%" not in js["notes"] or "Midpoint" in js["notes"]["40%"]
    assert not mismatches, f"{len(mismatches)} cells differ, e.g. {mismatches[:3]}"


def test_compute_uses_x4_midpoints():
    """``distill.compute`` fills 40%/60% with ``x4_midpoints`` (one rule)."""
    src = (ROOT / "distill.py").read_text(encoding="utf-8")
    body = src[src.index("def compute("):]
    assert "x4_midpoints(_convert_to_d86(d2887))" in body


def test_x4_midpoints_rule():
    d86 = {"30%": 100.01, "50%": 100.02, "70%": 101.0}
    out = distill.x4_midpoints(d86)
    assert out["40%"] == round((100.01 + 100.02) / 2, 2)
    assert out["60%"] == round((100.02 + 101.0) / 2, 2)
    assert "40%" not in d86                              # a new dict
    assert "60%" not in distill.x4_midpoints({"30%": 1.0, "50%": 2.0})
