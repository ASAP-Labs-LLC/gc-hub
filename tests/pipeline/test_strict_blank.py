"""distill.compute(strict_blank=True) (2A1 T2 review, CRITICAL 1): the hub must
never record a blank that was not subtracted. The default keeps v1's
behaviour (process_cdf byte-identical; the golden tests cover it)."""
from __future__ import annotations

from pipeline_helpers import hub  # noqa: F401  (the fixture)

from pathlib import Path
from unittest import mock

import pytest

import distill
import make_golden


@pytest.fixture()
def case(tmp_path):
    distill._CAL_CACHE.clear()
    inputs = make_golden.build_inputs(tmp_path)
    conf = make_golden.case_conf(tmp_path, inputs, "sample_40304_blank")
    cdf, blank = make_golden.case_paths(inputs, "sample_40304_blank")
    return inputs, conf, cdf, blank


def _hub_compute(cdf, conf, blank, **kw):
    return distill.compute(cdf, conf, blank, corrections={}, honour_env=False,
                           allow_auto=False, **kw)


def test_blank_applied_is_reported(case):
    _inputs, conf, cdf, blank = case
    assert _hub_compute(cdf, conf, blank)["blank_applied"] is True
    assert _hub_compute(cdf, conf, None)["blank_applied"] is False
    assert _hub_compute(cdf, conf, blank, strict_blank=True)["blank_applied"] is True
    # strict with a good blank: the same numbers as v1's mode
    assert (_hub_compute(cdf, conf, blank, strict_blank=True)["row"]
            == _hub_compute(cdf, conf, blank)["row"])


def test_unreadable_blank(case, tmp_path):
    _inputs, conf, cdf, _blank = case
    bad = tmp_path / "bad.CDF"
    bad.write_bytes(b"not a cdf")
    lax = _hub_compute(cdf, conf, bad)
    assert lax["blank_applied"] is False
    assert lax["row"] == _hub_compute(cdf, conf, None)["row"]
    with pytest.raises(distill.BlankUnreadable):
        _hub_compute(cdf, conf, bad, strict_blank=True)
    with pytest.raises(distill.BlankUnreadable):
        _hub_compute(cdf, conf, tmp_path / "missing.CDF", strict_blank=True)


def test_a_blank_carrying_sample_signal_is_rejected(case):
    inputs, conf, cdf, _blank = case
    signal = inputs["samples"]["40305"]           # a sample used as the "blank"
    lax = _hub_compute(cdf, conf, signal)
    assert lax["blank_applied"] is False
    with pytest.raises(distill.BlankRejected) as got:
        _hub_compute(cdf, conf, signal, strict_blank=True)
    assert "not a blank" in str(got.value)


def test_no_elution_window_after_subtraction_is_a_rejected_blank(case):
    _inputs, conf, cdf, blank = case
    real = distill._apply_blank_and_clip

    def clip(t, y, b):
        if b is not None:
            raise ValueError("No elution window found")
        return real(t, y, b)

    with mock.patch.object(distill, "_apply_blank_and_clip", clip):
        with pytest.raises(distill.BlankRejected) as got:
            _hub_compute(cdf, conf, blank, strict_blank=True)
        assert "No elution window" in str(got.value)
        assert _hub_compute(cdf, conf, blank)["blank_applied"] is False


def test_strict_blank_is_keyword_only_and_off_by_default(case):
    _inputs, conf, cdf, blank = case
    import inspect
    p = inspect.signature(distill.compute).parameters["strict_blank"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is False


# ── a genuine blank whose bleed is above every point of a weak sample ──────
# Found by the v1 differential replay (tests/replay): the blank passes the
# plausibility check and the relative-height guard (its bleed rises gently),
# but subtracting it leaves no signal at all, so the clipped chromatogram has
# zero area. v1 caught that and computed the sample with no blank; the hub
# re-raised the bare ValueError and left the sample in error.

def _bleed_blank_and_weak_sample(folder: Path):
    from datetime import datetime

    import numpy as np

    import cdf_fixtures as fx

    t = np.arange(0, fx.RUN_MIN, fx.DT_MIN)
    base = fx.gaussian(t, 0.30, 50000, 0.01) + 40 + 5 * t            # the fixture blank
    bleed = 1000.0 * np.clip((t - 0.5) / 3.5, 0.0, 1.0)               # +1000 pA, gently
    peaks = sum(fx.gaussian(t, c, 900.0, 0.006) for c in (5.0, 5.3, 5.6, 5.9))
    blank = fx.write_cdf(folder / "Blank3.CDF", t, base + bleed, "Blank3",
                         datetime(2026, 9, 25, 15, 20, 0), method_name="SIMDISTB.M")
    sample = fx.write_cdf(folder / "weak.CDF", t, base + peaks, "40399",
                          datetime(2026, 9, 25, 15, 25, 0), method_name="SIMDISB.M")
    return blank, sample


def test_a_blank_that_wipes_the_sample_out_is_a_rejected_blank(case, tmp_path):
    _inputs, conf, _cdf, _blank = case
    blank, sample = _bleed_blank_and_weak_sample(tmp_path)
    assert distill.is_plausible_blank(blank, distill.BLANK_MAX_INTENSITY_PA)
    with pytest.raises(distill.BlankRejected) as got:
        _hub_compute(sample, conf, blank, strict_blank=True)
    assert "zero integrated area" in str(got.value)
    # v1's mode: the blank skipped, the sample computed as with no blank
    lax = _hub_compute(sample, conf, blank)
    assert lax["blank_applied"] is False
    assert lax["row"] == _hub_compute(sample, conf, None)["row"]


def test_the_pipeline_computes_it_with_no_blank_and_notes_the_rejection(hub):
    import json

    import store

    hub.gc1()
    blank, sample = _bleed_blank_and_weak_sample(hub.src)
    bid = hub.submit(blank).sample_id
    assert hub.sample(bid)["is_blank"] == 1
    hub.worker().run_until_idle()
    sid = hub.submit(sample).sample_id
    hub.worker().run_until_idle()
    s = hub.sample(sid)
    assert s["status"] == "final", s["error"]
    rev = store.get_revision(sid, db=hub.db)
    assert rev["blank_used"] is None
    notes = json.loads(rev["notes"])
    assert notes["blank_rejected"]["sample_id"] == bid
    assert "zero integrated area" in notes["blank_rejected"]["reason"]
    expected = distill.compute(sample, dict(hub.conf), None, honour_env=False)["row"]
    got = json.loads(rev["results"])
    numeric = [k for k in expected if k.startswith(("2887 ", "D86 "))]
    assert [got[k] for k in numeric] == [expected[k] for k in numeric]
