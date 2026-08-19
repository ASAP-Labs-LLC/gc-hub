"""Parse and resolve reprocess queries.

A query is free text the user types into the Re-process box: single Lab IDs,
comma/newline separated lists, and integer ranges. Ranges use ``to``, ``-``
or ``:`` and are expanded over the *visible* (integer) Lab ID shown in the
sample library — e.g. ``34562 to 34569`` reprocesses every sample whose
visible ID falls in [34562, 34569].

Kept as pure functions (no Flask, no disk) so they are unit-testable and the
preview route and confirm path share one source of truth.
"""

from __future__ import annotations

import re

# Guard against a fat-finger range (e.g. "1 to 99999999") iterating forever.
MAX_RANGE_SPAN = 100_000

# A range token: <int> <sep> <int>, sep is "to", "-" or ":" (optional spaces).
_RANGE_RE = re.compile(
    r"^\s*(\d+)\s*(?:to|[-:])\s*(\d+)\s*$",
    re.IGNORECASE,
)


def parse_reprocess_query(text: str) -> list[dict]:
    """Split *text* into ordered tokens.

    Each token is ``{"kind": "single", "value": str}`` or
    ``{"kind": "range", "start": int, "end": int}`` (start <= end).
    Raises ``ValueError`` if a range spans more than ``MAX_RANGE_SPAN``.
    """
    tokens: list[dict] = []
    for raw in re.split(r"[\n,]+", text or ""):
        piece = raw.strip()
        if not piece:
            continue
        m = _RANGE_RE.match(piece)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if start > end:
                start, end = end, start
            if end - start + 1 > MAX_RANGE_SPAN:
                raise ValueError(
                    f"Range {start}-{end} is too large (>{MAX_RANGE_SPAN} IDs)"
                )
            tokens.append({"kind": "range", "start": start, "end": end})
        else:
            tokens.append({"kind": "single", "value": piece})
    return tokens


def resolve_query(tokens: list[dict], library_names: list[str]) -> dict:
    """Resolve *tokens* against the library's visible Lab IDs.

    Returns ``{"matched": [...library names...], "missing": [...ids...]}``,
    both deduped preserving first-seen order. ``matched`` holds the actual
    library names (so reprocess can resolve them); ``missing`` holds the
    queried IDs that no sample matched.
    """
    # Map integer value -> library name, for range / zero-padding matching.
    int_to_name: dict[int, str] = {}
    name_set = set(library_names)
    for name in library_names:
        try:
            int_to_name.setdefault(int(name), name)
        except (TypeError, ValueError):
            continue

    matched: list[str] = []
    missing: list[str] = []
    seen_matched: set[str] = set()
    seen_missing: set[str] = set()

    def _add_matched(name: str) -> None:
        if name not in seen_matched:
            seen_matched.add(name)
            matched.append(name)

    def _add_missing(value: str) -> None:
        if value not in seen_missing:
            seen_missing.add(value)
            missing.append(value)

    for tok in tokens:
        if tok["kind"] == "single":
            value = tok["value"]
            if value in name_set:
                _add_matched(value)
                continue
            try:
                ival = int(value)
            except (TypeError, ValueError):
                _add_missing(value)
                continue
            if ival in int_to_name:
                _add_matched(int_to_name[ival])
            else:
                _add_missing(value)
        else:  # range
            for i in range(tok["start"], tok["end"] + 1):
                if i in int_to_name:
                    _add_matched(int_to_name[i])
                else:
                    _add_missing(str(i))

    return {"matched": matched, "missing": missing}
