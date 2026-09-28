"""ASTM D2887 (simulated distillation) with the D86 correlation: ``distill.compute``
in hub mode, with no numeric change.

Hub mode: ``GC_CAL_CDF`` is ignored (``honour_env=False``), an unusable
assigned calibration raises instead of silently auto-detecting
(``allow_auto=False``), and the corrections are always passed explicitly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import distill

NAME = "D2887"


def compute(cdf_path, ctx: Dict[str, str], *, blank_path: Optional[Path],
            corrections: Dict[str, float]) -> dict:
    """``distill.compute(cdf_path, ctx, blank_path, corrections=..., honour_env=False,
    allow_auto=False)``. ``corrections`` is required; ``None`` raises
    ``ValueError`` (the hub never loads corrections implicitly)."""
    if corrections is None:
        raise ValueError("D2887 needs explicit corrections (the hub never loads them implicitly)")
    return distill.compute(cdf_path, ctx, blank_path, corrections=corrections,
                           honour_env=False, allow_auto=False)
