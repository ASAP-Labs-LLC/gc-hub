"""Presentation helpers for the sample library list.

The library shows one entry per *injection* — i.e. per (Lab ID,
InjectionDateTime) — so re-runs of the same sample name (including multiple
runs in the same day) are all visible, not collapsed to the latest. This
module assigns each entry a stable unique id and a display label: repeated
names get a run-order counter suffix ("AF25", "AF25 (2)", "AF25 (3)").

Pure (no Flask, no disk) so it is unit-testable and independent of the cache
build. The raw ``name`` is never modified — only ``display_name`` carries the
suffix — so CSV export, QBench lookups and reprocess keep using the bare
sample ID.
"""

from __future__ import annotations


def assign_duplicate_labels(entries: list[dict]) -> list[dict]:
    """Set ``uid`` and ``display_name`` on each entry, in place.

    *entries* is a list of dicts with at least ``name`` and ``mtime`` (and
    usually ``inj_dt``). Input order is preserved. The counter suffix is
    assigned chronologically by ``mtime`` (oldest run = bare name), so it
    reflects true run order regardless of how the list is later sorted.
    """
    # Group indices by name to detect repeats and order them chronologically.
    by_name: dict[str, list[int]] = {}
    for idx, e in enumerate(entries):
        by_name.setdefault(e.get("name", ""), []).append(idx)

    ordinal: dict[int, int] = {}
    for name, idxs in by_name.items():
        # Stable chronological order: oldest first; ties broken by inj_dt then
        # original position so the numbering is deterministic.
        ordered = sorted(
            idxs,
            key=lambda i: (entries[i].get("mtime", 0.0),
                           entries[i].get("inj_dt", ""),
                           i),
        )
        for run_no, i in enumerate(ordered, start=1):
            ordinal[i] = run_no

    seen_uids: set[str] = set()
    for idx, e in enumerate(entries):
        name = e.get("name", "")
        run_no = ordinal.get(idx, 1)
        e["display_name"] = name if run_no == 1 else f"{name} ({run_no})"

        base_uid = f"{name}|{e.get('inj_dt', '')}"
        uid = base_uid
        bump = 1
        while uid in seen_uids:
            bump += 1
            uid = f"{base_uid}#{bump}"
        seen_uids.add(uid)
        e["uid"] = uid

    return entries
