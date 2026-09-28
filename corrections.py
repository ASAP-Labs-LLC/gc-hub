"""D86 correction factors for the hub: hub-owned, per instrument.

Design (Ryan, 2026-09-28): the GC correction factors live IN THE HUB, one set
of eleven per instrument, edited on the Instruments page. LEM holds none for
the GCs and only forwards results. The phase-1 JSON file is read once, to seed
``gc1``.

A correction is added to a reported D86 temperature, so "no correction" is a
claim about the result, never a default for "not set" or "could not read".
:class:`StoreProvider` returns all eleven cuts or raises
:class:`CorrectionsUnavailable`, which the pipeline turns into
``pending_corrections`` ("Corrections not set for GC-2").

Stdlib only; importable anywhere.
"""
from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Protocol

D86_CUTS = ["IBP", "5%", "10%", "20%", "30%", "50%", "70%", "80%", "90%", "95%", "FBP"]

# The editor's sanity bound, °C. A D86 correction is a few degrees (V4's
# Agilent offsets were -3.5 to -12.1); anything past this is a typo.
MAX_ABS_CORRECTION_C = 50.0

# The phase-1 corrections file's test names -> D86 cut, used only to seed
# gc1. Mirrors distill._D86_CORRECTION_TEST_MAP (a test pins them).
PHASE1_FILE_MAP: Dict[str, str] = {
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


@dataclass(frozen=True)
class Corrections:
    source: str            # 'hub' | 'file' | 'legacy'
    fetched_at: str        # ISO with offset. 'hub': when these values were saved
                           # (the store's updated_at); 'file': when it was read.
    values: dict           # cut -> float; 'hub' and 'file' always hold all eleven
    updated_by: str = ""   # 'hub': who saved them


class CorrectionsUnavailable(Exception):
    """No usable corrections: the sample goes ``pending_corrections``.
    ``kind`` is always ``'config'``: a person has to set or fix them."""

    def __init__(self, reason: str, kind: str = "config") -> None:
        if kind != "config":
            raise ValueError(f"CorrectionsUnavailable kind must be 'config', not {kind!r}")
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


class CorrectionsProvider(Protocol):
    def get(self, instrument: dict) -> Corrections: ...


# ── pure helpers ────────────────────────────────────────────────────────────

def _is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def validate_values(values) -> List[str]:
    """Everything wrong with a cut->value set, one sentence each; [] if valid.

    Valid means: exactly the eleven D86 cuts, each a finite number (int or
    float; not a string, not a bool) within ±MAX_ABS_CORRECTION_C °C."""
    if not isinstance(values, dict):
        return ["Corrections must be a set of eleven D86 cut values."]
    errors: List[str] = []
    missing = [cut for cut in D86_CUTS if cut not in values]
    if missing:
        errors.append(f"Missing a correction for {', '.join(missing)}.")
    unknown = [str(k) for k in values if k not in D86_CUTS]
    if unknown:
        errors.append(f"Not a D86 cut: {', '.join(unknown)}.")
    for cut in D86_CUTS:
        if cut not in values:
            continue
        value = values[cut]
        if not _is_number(value):
            errors.append(f"{cut}: {value!r} is not a number.")
        elif abs(value) > MAX_ABS_CORRECTION_C:
            errors.append(f"{cut}: {value:g} °C is beyond ±{MAX_ABS_CORRECTION_C:g} °C.")
    return errors


def values_differ(a: dict, b: dict) -> bool:
    """True if two cut->value sets would correct any result differently. A cut
    missing from either counts as 0.0, which is how the pipeline applies it."""
    a, b = dict(a or {}), dict(b or {})
    for cut in set(a) | set(b):
        x, y = a.get(cut, 0.0), b.get(cut, 0.0)
        if not (_is_number(x) and _is_number(y)) or abs(x - y) > 1e-9:
            return True
    return False


def _in_cut_order(values: dict) -> Dict[str, float]:
    return {cut: float(values[cut]) for cut in D86_CUTS if cut in values}


# ── the hub's own store ─────────────────────────────────────────────────────

class StoreProvider:
    """The instrument's saved corrections, via ``read_fn(instrument_id)``,
    which returns ``{"values": {cut: float}, "updated_at": str,
    "updated_by": str}`` or ``None`` when none have been saved.

    ``None`` raises "Corrections not set for <name>"; a record that is not
    all eleven valid values (``validate_values``) or has no ``updated_at``
    raises too. An exception from ``read_fn`` (a database error) propagates:
    it is not "not set"."""

    def __init__(self, read_fn: Callable[[str], Optional[dict]]) -> None:
        self._read = read_fn

    def get(self, instrument: dict) -> Corrections:
        instrument = instrument or {}
        inst_id = str(instrument.get("id") or "").strip()
        if not inst_id:
            raise CorrectionsUnavailable("The instrument has no id.")
        name = str(instrument.get("name") or "").strip() or inst_id
        record = self._read(inst_id)
        if record is None:
            raise CorrectionsUnavailable(f"Corrections not set for {name}")
        if not isinstance(record, dict):
            raise CorrectionsUnavailable(f"The saved corrections for {name} are unreadable.")
        errors = validate_values(record.get("values"))
        if errors:
            raise CorrectionsUnavailable(f"The saved corrections for {name} are not "
                                         f"usable: {' '.join(errors)}")
        updated_at = str(record.get("updated_at") or "").strip()
        if not updated_at:
            raise CorrectionsUnavailable(f"The saved corrections for {name} do not say "
                                         f"when they were set.")
        return Corrections(source="hub", fetched_at=updated_at,
                           values=_in_cut_order(record["values"]),
                           updated_by=str(record.get("updated_by") or ""))


# ── the phase-1 file (seeding gc1 only) ─────────────────────────────────────

FILE_SECTION = "Agilent GC"
FILE_INSTRUMENT_ID = "gc1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FileProvider:
    """Phase-1 ``correction_factors_json``: ``{"Agilent GC": {test_name:
    {"correction_value": x}, ...}, ...}``, for instrument ``gc1`` only.

    Stricter than ``distill.load_d86_corrections``, which answered ``{}``
    (= "no correction") for every failure: a missing or unreadable file, bad
    JSON, a missing/non-object section, a section naming none of the phase-1
    tests, a mapped test without a finite ``correction_value``, or a value
    beyond the sanity bound all raise. Kept from phase 1: a cut the file does
    not list is uncorrected, recorded as an explicit 0.0.
    """

    def __init__(self, json_path: str, *, clock: Optional[Callable[[], datetime]] = None) -> None:
        self.json_path = str(json_path)
        self._clock = clock or _utc_now
        self._lock = threading.Lock()

    def get(self, instrument: dict) -> Corrections:
        inst_id = str((instrument or {}).get("id") or "")
        if inst_id != FILE_INSTRUMENT_ID:
            raise CorrectionsUnavailable(f"The corrections file serves only instrument "
                                         f"{FILE_INSTRUMENT_ID!r}, not {inst_id!r}.")
        with self._lock:
            fetched_at = self._clock().astimezone(timezone.utc).isoformat(timespec="seconds")
            section = self._read_section()
        values: Dict[str, float] = {}
        found = 0
        for test_name, cut in PHASE1_FILE_MAP.items():
            if test_name not in section:
                values[cut] = 0.0
                continue
            entry = section[test_name]
            number = entry.get("correction_value") if isinstance(entry, dict) else None
            if isinstance(number, str):
                try:
                    number = float(number)
                except ValueError:
                    number = None
            if not _is_number(number):
                raise CorrectionsUnavailable(f"The corrections file's {test_name!r} entry "
                                             f"has no usable correction_value ({entry!r}).")
            values[cut] = float(number)
            found += 1
        if not found:
            raise CorrectionsUnavailable(f"The {FILE_SECTION!r} section of {self.json_path} "
                                         f"lists none of the phase-1 test names; this is "
                                         f"not the corrections file.")
        errors = validate_values(values)
        if errors:
            raise CorrectionsUnavailable(f"The corrections file {self.json_path}: "
                                         f"{' '.join(errors)}")
        return Corrections(source="file", fetched_at=fetched_at, values=_in_cut_order(values))

    def _read_section(self) -> dict:
        try:
            with open(self.json_path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            raise CorrectionsUnavailable(
                f"The corrections file {self.json_path} does not exist.") from None
        except OSError as exc:
            raise CorrectionsUnavailable(
                f"The corrections file {self.json_path} could not be read: {exc}.") from None
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise CorrectionsUnavailable(
                f"The corrections file {self.json_path} is not valid JSON: {exc}.") from None
        if not isinstance(data, dict):
            raise CorrectionsUnavailable(
                f"The corrections file {self.json_path} is not a JSON object.")
        section = data.get(FILE_SECTION)
        if not isinstance(section, dict):
            raise CorrectionsUnavailable(
                f"The corrections file {self.json_path} has no {FILE_SECTION!r} section.")
        return section


def seed_from_file(json_path: str) -> Dict[str, float]:
    """The eleven cut values to seed ``gc1`` from the phase-1 file (a new
    dict, in cut order). Raises :class:`CorrectionsUnavailable` under the
    FileProvider's rules."""
    return dict(FileProvider(json_path).get({"id": FILE_INSTRUMENT_ID}).values)
