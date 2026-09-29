"""v2.0.0 RC: the heartbeat's ``state`` is one of contract §1's values; any
other string (a newer agent's state) is stored as ``unknown`` rather than
refused, so a newer agent never breaks its own heartbeat."""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

import ingest_api  # noqa: E402


@pytest.mark.parametrize("state", ["sending", "idle", "paused", "hub-unreachable",
                                   "auth-error", "config-error"])
def test_contract_states_are_kept(state):
    assert ingest_api._heartbeat_values({"state": state})["state"] == state


@pytest.mark.parametrize("state", ["warp-drive", "IDLE", "", " idle", pytest.param("x" * 5000, id="long")])
def test_other_states_are_stored_as_unknown(state):
    assert ingest_api._heartbeat_values({"state": state})["state"] == "unknown"


def test_a_missing_state_stays_missing():
    assert ingest_api._heartbeat_values({})["state"] is None


def test_a_non_string_state_is_still_refused():
    with pytest.raises(ValueError):
        ingest_api._heartbeat_values({"state": 3})


def test_the_states_match_the_contract():
    assert ingest_api.AGENT_STATES == ("sending", "idle", "paused", "hub-unreachable",
                                       "auth-error", "config-error")
