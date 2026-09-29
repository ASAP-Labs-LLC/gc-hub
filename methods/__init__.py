"""Hub methods registry (phase 2).

A *hub method* (``instruments.method``, the values of an instrument's
``method_map``) names the analysis a sample gets. ``get(name)`` returns an
object with::

    NAME: str
    compute(cdf_path, ctx, *, blank_path, corrections) -> dict

``ctx`` is ``instruments.context(...)`` (the merged conf); ``corrections`` is
the cut -> value dict to add, always passed explicitly. D2887 is the only
method today (``methods/d2887.py``).

The CDF's ChemStation method name (``detection_method_name``) is compared
with ``normalise_method_name``: trimmed, directory part stripped,
upper-cased.
"""
from __future__ import annotations

from typing import Mapping

import distill

from . import d2887

normalise_method_name = distill.normalise_method_name

_REGISTRY = {d2887.NAME: d2887}


class UnknownMethod(KeyError):
    """No hub method by that name."""


def _key(name) -> str:
    return str(name or "").strip().upper()


def get(name):
    """The hub method registered as ``name`` (case-insensitive); ``UnknownMethod`` if none."""
    try:
        return _REGISTRY[_key(name)]
    except KeyError:
        raise UnknownMethod(f"no hub method {name!r}") from None


def names() -> list:
    return sorted(_REGISTRY)


def names_mapped_to(method_map: Mapping[str, str], hub_method: str) -> list:
    """The ChemStation method names ``method_map`` sends to ``hub_method``, sorted."""
    want = _key(hub_method)
    return sorted(n for n, m in (method_map or {}).items() if _key(m) == want)
