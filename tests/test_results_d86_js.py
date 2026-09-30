"""The Results page's "Corrected D86" off (v5.0 lane R) converts each row's
stored D2887 cells in the browser (``static/js/results_logic.js``
``convertToD86``): it must give exactly what ``distill.compute`` stores as
``d86_uncorrected`` (the App. X4 cuts, 40 %/60 % by ``distill.x4_midpoints``,
rounded like Python's ``round(x, 2)``). Node runs the real module on random
rows and ties; skipped without node.
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

RUNNER = r"""
const fs = require('fs');
const R = require(process.argv[1] + '/static/js/results_logic.js');
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
process.stdout.write(JSON.stringify(cases.map((c) => R.convertToD86(c))));
"""


def _cases(n: int) -> list:
    rng = random.Random(20260930)
    cases = [dict(zip(LABELS, sorted(round(rng.uniform(20, 600), 2) for _ in range(13))))
             for _ in range(n)]
    for base in (100.0, 187.5, 250.25, 333.33):
        for step in (0.01, 0.03, 0.05, 0.125, 0.375):
            cases.append(dict(zip(LABELS, [round(base + i * step * 7, 2) for i in range(13)])))
    return cases


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_results_conversion_matches_python(tmp_path):
    cases = _cases(30000)
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(cases), encoding="utf-8")
    out = subprocess.run([NODE, "-e", RUNNER, str(ROOT), str(f)], capture_output=True,
                         text=True, check=True, timeout=120).stdout
    got = json.loads(out)
    bad = []
    for case, js in zip(cases, got):
        py = distill.x4_midpoints(distill._convert_to_d86(case))
        for label in LABELS:
            if js.get(label) != py.get(label):
                bad.append((label, py.get(label), js.get(label)))
    assert not bad, f"{len(bad)} cells differ, e.g. {bad[:3]}"


def test_the_coefficients_are_distills():
    """The module's App. X4 table is distill's, cut for cut."""
    src = (ROOT / "static" / "js" / "results_logic.js").read_text(encoding="utf-8")
    for cut, coeffs in distill._CONVERSION_COEFF.items():
        key = cut if cut in ("IBP", "FBP") else f"'{cut}'"
        line = next(ln for ln in src.splitlines() if ln.strip().startswith(f"{key}: ["))
        nums = [float(x) for x in line.split("[", 1)[1].split("]", 1)[0].split(",")]
        assert nums == list(coeffs), cut
