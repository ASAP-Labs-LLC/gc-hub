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


# ── shared helpers ───────────────────────────────────────────────────────────

Clock = Callable[[], datetime]


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _finite_float(value) -> Optional[float]:
    """A real, finite number, or None. Booleans are not numbers here."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def values_differ(a: dict, b: dict) -> bool:
    """True if two cut->value maps would correct any result differently."""
    a, b = dict(a or {}), dict(b or {})
    if set(a) != set(b):
        return True
    for cut in a:
        x, y = _finite_float(a[cut]), _finite_float(b[cut])
        if x is None or y is None or abs(x - y) > 1e-9:
            return True
    return False


# ── the phase-1 file (2A1) ───────────────────────────────────────────────────

FILE_SECTION = "Agilent GC"
FILE_INSTRUMENT_ID = "gc1"


class FileProvider:
    """Phase-1 ``correction_factors_json``: ``{"Agilent GC": {test_name:
    {"correction_value": x}, ...}, ...}``. Serves only instrument ``gc1``.

    Stricter than ``distill.load_d86_corrections`` in exactly the ways that
    function hid a failure as "no correction":

    - missing file, unparseable JSON, a missing or non-object section, or a
      section with **no** mapped test -> ``config``;
    - a mapped test whose ``correction_value`` is not a finite number ->
      ``config`` (phase 1 dropped the whole file for this);
    - any other OS error (share offline, permissions) -> ``unreachable``.

    Kept from phase 1: a mapped cut the file does not list is uncorrected, so
    it is recorded as an explicit 0.0.
    """

    def __init__(self, json_path: str, *, clock: Optional[Clock] = None) -> None:
        self.json_path = str(json_path)
        self._clock = clock or datetime.now
        self._latest: Dict[str, Corrections] = {}

    def get(self, instrument: dict) -> Corrections:
        inst_id = str((instrument or {}).get("id") or "")
        if inst_id != FILE_INSTRUMENT_ID:
            raise _config(f"The corrections file serves only instrument "
                          f"{FILE_INSTRUMENT_ID!r}, not {inst_id!r}; set up LEM "
                          f"corrections for it.")
        mapping = parse_correction_map((instrument or {}).get("correction_map"))
        fetched_at = _iso(self._clock())
        section = self._read_section()
        values: Dict[str, float] = {}
        found = 0
        for test_name, cut in mapping.items():
            if test_name not in section:
                values[cut] = 0.0
                continue
            entry = section[test_name]
            number = (_finite_float(entry.get("correction_value"))
                      if isinstance(entry, dict) else None)
            if number is None:
                raise _config(f"The corrections file's {test_name!r} entry has no "
                              f"usable correction_value ({entry!r}).")
            values[cut] = number
            found += 1
        if not found:
            raise _config(f"The {FILE_SECTION!r} section of {self.json_path} lists "
                          f"none of the mapped tests ({', '.join(mapping)}); this "
                          f"is not the corrections file.")
        result = Corrections(source="file", fetched_at=fetched_at,
                             values=_in_cut_order(values))
        self._latest[inst_id] = result
        return result

    def refresh(self, instrument: dict) -> Corrections:
        return self.get(instrument)

    def changed_since(self, instrument_id: str, used: Corrections) -> bool:
        latest = self._latest.get(str(instrument_id))
        return latest is not None and values_differ(latest.values, used.values)

    def _read_section(self) -> dict:
        try:
            with open(self.json_path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            raise _config(f"The corrections file {self.json_path} does not exist.") from None
        except OSError as exc:
            raise _unreachable(f"The corrections file {self.json_path} could not be "
                               f"read: {exc}.") from None
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise _config(f"The corrections file {self.json_path} is not valid "
                          f"JSON: {exc}.") from None
        if not isinstance(data, dict):
            raise _config(f"The corrections file {self.json_path} is not a JSON object.")
        section = data.get(FILE_SECTION)
        if not isinstance(section, dict):
            raise _config(f"The corrections file {self.json_path} has no "
                          f"{FILE_SECTION!r} section.")
        return section


def _in_cut_order(values: Dict[str, float]) -> Dict[str, float]:
    return {cut: values[cut] for cut in D86_CUTS if cut in values}
