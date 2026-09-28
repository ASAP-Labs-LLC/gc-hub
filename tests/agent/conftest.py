"""Fixtures for the agent suite. The agent package lives in ``agent/`` and is
imported from there (it never imports hub modules)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO / "agent"
HERE = Path(__file__).resolve().parent
for p in (str(AGENT_DIR), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fakehub import FakeHub  # noqa: E402


@pytest.fixture
def hub():
    h = FakeHub().start()
    yield h
    h.stop()


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock():
    return FakeClock()
