"""Edge cases of receiving a CDF (``pipeline.submit``) found by the
stability gauntlet: a file's content must never make ``submit`` raise
anything but ``SubmitRejected`` (a 400 the agent sets aside). Any other
exception is a 500 at ``/api/ingest``, which the agent retries forever with
the file at the head of its queue, so nothing else from that GC arrives."""
from __future__ import annotations

from pipeline_helpers import SIMDIS, hub  # noqa: F401  (hub: the fixture)

from datetime import datetime

import numpy as np
import pytest

import cdf_fixtures as fx
import distill


@pytest.mark.parametrize("times", [np.zeros(4000), np.full(4000, np.nan)],
                         ids=["zero-interval", "nan-interval"])
def test_blank_named_file_with_a_degenerate_time_axis_is_received(hub, times):
    """A blank-named CDF whose sampling interval is 0 or NaN (a corrupt
    file) used to raise ZeroDivisionError / ValueError out of the blank
    check at receive time. It is not a plausible blank; it is received."""
    hub.gc1()
    p = fx.write_cdf(hub.src / "blank-bad-axis.CDF", times, np.full(times.size, 50.0), "Blank",
                     datetime(2026, 9, 24, 15, 30, 27), method_name=SIMDIS)
    res = hub.submit(p)
    assert res.outcome == "created"
    assert hub.sample(res.sample_id)["is_blank"] == 0


def test_is_plausible_blank_is_false_for_a_degenerate_time_axis(tmp_path):
    p = fx.write_cdf(tmp_path / "b.CDF", np.zeros(100), np.full(100, 50.0), "Blank",
                     datetime(2026, 9, 24, 15, 30, 27))
    assert distill.is_plausible_blank(p, 200.0) is False


def test_blank_check_window_is_capped_at_the_trace_length(monkeypatch):
    """The rolling-minimum window was 1 minute in samples, uncapped: a file
    claiming a tiny sampling interval (1e-9 s) asked scipy for a window of
    ~6e10 points and the allocation took the hub process down (killed by
    the OS, or MemoryError), at receive time for a blank-named file and in
    the Worker for any sample a blank is subtracted from. A window of
    2n + 1 already covers the whole trace from every point (edge padding
    adds no new values), so capping there changes no result."""
    import distill as d
    real = d.minimum_filter1d
    sizes = []

    def spy(y, size, **kw):
        sizes.append(size)
        return real(y, size=size, **kw)

    monkeypatch.setattr(d, "minimum_filter1d", spy)
    rng = np.random.default_rng(7)
    y = rng.random(500) * 100
    t_tiny = np.arange(500) * 1e-7          # minutes: a 1-minute window of 1e7 points
    capped = d._peak_height_outside_solvent(t_tiny + 1.0, y)
    assert sizes[-1] <= 2 * y.size + 1
    # same answer as the global minimum the huge window meant
    expected = float((y - y.min()).max())
    assert capped == pytest.approx(expected)
    # an ordinary trace is unchanged (window smaller than the trace)
    t = np.arange(8000) * 0.001
    yy = fx.gaussian(t, 3.2, 1200, 0.9) + 40 + 5 * t
    monkeypatch.setattr(d, "minimum_filter1d", real)
    base = d._peak_height_outside_solvent(t, yy)
    win = max(3, int(round(1.0 / 0.001)))
    resid = yy - real(yy, size=win, mode="nearest")
    assert base == float(resid[t > d.BLANK_SOLVENT_END_MIN].max())
