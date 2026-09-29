"""Build ``diesel_pair.npz``: a realistic synthetic diesel sample + standard.

No real diesel sample/standard pair was available (the 2026-09-25 share
snapshot holds only the ``Feb24 n-alkanes`` calibration CDF and a blank), so
the pair is synthetic but modelled on real data:

* the time axis is the calibration CDF's (0.000833 min steps, 0–8.57 min);
* the ladder is that CDF's n-alkane peak times, carbons assigned by spacing;
* each chromatogram is an unresolved hump (C9–C26, centred near C15) with a
  resolved n-alkane peak at every carbon on top, a rising baseline and
  detector noise;
* the "sample" is another diesel batch: every peak 0.002 min later, peak
  heights varied by up to ±30% (``jitter=0.3``), a 1% heavier hump and its
  own noise. That is the everyday case that gave operators dozens of
  per-excursion bullets.

``batch`` and ``with_gasoline`` build the review's variants (other retention
shifts and jitters; the same diesel with 1–10% gasoline) for
``tests/test_bullets_regression.py``.

No customer identifiers. Deterministic (fixed seed). Run from the repo root:
``python tests/fixtures/make_diesel_pair.py``.
"""
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent / "diesel_pair.npz"

# Peak times of the real Feb24 n-alkanes calibration CDF (min) and the carbon
# numbers they were assigned by spacing (C12, C18, C20, C21 … are unresolved).
LADDER_T = [0.183, 0.234, 0.335, 0.510, 0.767, 1.084, 1.430, 2.131, 2.459, 2.773,
            3.073, 3.360, 3.892, 4.832, 5.637, 6.341, 6.987, 7.602, 8.192]
LADDER_C = [5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 19, 22, 25, 28, 30, 32, 34]


def c2t(c):
    return np.interp(c, LADDER_C, LADDER_T)


def chromatogram(t, rng, *, hump_scale=1.0, shift=0.0, jitter=0.0):
    """One diesel run. *jitter* varies every peak's height (batch
    composition), *shift* delays every peak (retention drift)."""
    y = 60.0 + 25.0 * t                                    # baseline drift
    hump_c = np.linspace(7, 30, 400)
    hump_w = np.exp(-0.5 * ((hump_c - 15.5) / 4.2) ** 2)
    y += hump_scale * 9000.0 * np.interp(t, c2t(hump_c), hump_w)
    for c in range(8, 31):                                 # resolved n-alkanes
        h = 16000.0 * np.exp(-0.5 * ((c - 15.0) / 5.0) ** 2)
        h *= 1.0 + jitter * rng.uniform(-1, 1)
        y += h * np.exp(-0.5 * ((t - c2t(c) - shift) / 0.008) ** 2)
    product = np.random.default_rng(7)                     # the product's isomer pattern
    for c in np.arange(8.1, 28, 0.2):                      # minor isomer peaks
        h = 1500.0 * product.uniform(0.3, 1.0) * (1.0 + jitter * rng.uniform(-1, 1))
        y += h * np.exp(-0.5 * ((t - c2t(c) - shift) / 0.0064) ** 2)
    y += rng.normal(0, 18.0, t.size)                       # detector noise
    return y


AXIS = dict(n=10287, dt=0.0008333333457510861, t0=0.00073853333791097)
STANDARD_SEED = 20260925


def axis():
    return np.arange(AXIS["n"]) * AXIS["dt"] + AXIS["t0"]


def standard(t):
    return chromatogram(t, np.random.default_rng(STANDARD_SEED))


def batch(t, seed, *, shift=0.002, jitter=0.3, hump=1.01):
    """Another batch of the same diesel."""
    return chromatogram(t, np.random.default_rng(seed), hump_scale=hump, shift=shift,
                        jitter=jitter)


def _gasoline_components():
    rng = np.random.default_rng(3)
    return rng.uniform(5.0, 10.5, 22), rng.uniform(0.25, 1.0, 22) * 40000   # neat scale


def with_gasoline(t, frac, *, seed=12, shift=0.002):
    """A diesel batch diluted with *frac* gasoline: 22 resolved components
    between C5 and C10.5 (some co-eluting with the n-alkanes)."""
    y = (1 - frac) * batch(t, seed, shift=shift)
    for c, h in zip(*_gasoline_components()):
        y += frac * h * np.exp(-0.5 * ((t - c2t(c)) / 0.006) ** 2)
    return y


def build():
    t = axis()
    std = standard(t)
    sample = chromatogram(t, np.random.default_rng(20260929), hump_scale=1.01,
                          shift=0.002, jitter=0.3)
    np.savez_compressed(OUT, t=t, sample=sample.astype(np.float32),
                        standard=std.astype(np.float32),
                        ladder_t=np.array(LADDER_T), ladder_c=np.array(LADDER_C))
    return OUT


if __name__ == "__main__":
    print(build())
