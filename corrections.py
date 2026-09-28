"""D86 correction factors for the hub: where they come from, and when to refuse.

Contract: ``docs/superpowers/specs/2026-09-28-phase2-contracts.md`` §2.
Rules: the design's "LEM corrections (2C)".

A correction is added to a reported D86 temperature, so "no correction" is a
claim about the result, never a default for "could not find out". Every
provider here either returns a value for **every** cut in the instrument's
map (0.0 only where the source says zero) or raises
:class:`CorrectionsUnavailable`, which the pipeline turns into
``pending_corrections``. Nothing here ever returns an empty dict.

Stdlib only at import time (``requests`` is imported when the default HTTP
getter is first used), so the module is importable anywhere and unit-tested
without the web stack.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Optional, Protocol
from urllib.parse import quote

LOGGER = logging.getLogger(__name__)

D86_CUTS = ["IBP", "5%", "10%", "20%", "30%", "50%", "70%", "80%", "90%", "95%", "FBP"]

# LEM test_name -> D86 cut. Mirrors distill._D86_CORRECTION_TEST_MAP (a test
# pins the two together).
DEFAULT_CORRECTION_MAP: Dict[str, str] = {
    "IBP - D86": "IBP",
    "5% - D86": "5%",
    "10% - D86": "10%",
    "20% - D86": "20%",
    "30% - D86": "30%",
    "50% - D86": "50%",
    "70% - D86": "70%",
    "80% - D86": "80%",
    "90% - D86": "90%",
    "95% - D86": "95%",
    "FBP - D86": "FBP",
}

KINDS = ("unreachable", "config")


@dataclass(frozen=True)
class Corrections:
    source: str        # 'lem' | 'cache' | 'file'
    fetched_at: str    # ISO, when the values were fetched from their source
    values: dict       # cut -> float, for EVERY cut in the map (explicit 0.0 allowed)


class CorrectionsUnavailable(Exception):
    """No trustworthy corrections: the sample goes ``pending_corrections``.

    ``kind`` is ``'unreachable'`` (the source could not be asked; retried) or
    ``'config'`` (the source answered, or the setup is such, that a human
    must act: unknown machine, unmapped test, bad units, bad map, bad file).
    """

    def __init__(self, reason: str, kind: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"CorrectionsUnavailable kind must be one of {KINDS}, not {kind!r}")
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


class CorrectionsProvider(Protocol):
    def get(self, instrument: dict) -> Corrections: ...
    def refresh(self, instrument: dict) -> Corrections: ...


def _config(reason: str) -> CorrectionsUnavailable:
    return CorrectionsUnavailable(reason, "config")


def _unreachable(reason: str) -> CorrectionsUnavailable:
    return CorrectionsUnavailable(reason, "unreachable")


def parse_correction_map(raw) -> Dict[str, str]:
    """An instrument's ``correction_map`` (JSON string, or None = default).

    Returns a fresh dict ``{test_name: cut}``. Anything doubtful raises
    ``config``: it is never repaired, because a repaired map applies a
    correction to the wrong temperature without anyone seeing it.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return dict(DEFAULT_CORRECTION_MAP)
    if not isinstance(raw, str):
        raise _config("The correction map must be stored as JSON text, not "
                      f"{type(raw).__name__}.")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise _config(f"The correction map is not valid JSON: {exc}.") from None
    if not isinstance(data, dict) or not data:
        raise _config("The correction map must be a non-empty JSON object of "
                      "LEM test name -> D86 cut.")
    out: Dict[str, str] = {}
    seen: Dict[str, str] = {}
    for name, cut in data.items():
        name = str(name).strip()
        if not name:
            raise _config("The correction map has a blank LEM test name.")
        if not isinstance(cut, str) or cut.strip() not in D86_CUTS:
            raise _config(f"The correction map sends {name!r} to {cut!r}, which is "
                          f"not a D86 cut ({', '.join(D86_CUTS)}).")
        cut = cut.strip()
        if cut in seen:
            raise _config(f"The correction map sends both {seen[cut]!r} and {name!r} "
                          f"to {cut}; one cut can take only one correction.")
        seen[cut] = name
        out[name] = cut
    return out
