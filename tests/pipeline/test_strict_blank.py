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
