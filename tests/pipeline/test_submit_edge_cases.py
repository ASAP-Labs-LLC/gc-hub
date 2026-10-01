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
