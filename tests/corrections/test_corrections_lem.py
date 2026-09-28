"""LemProvider against LEM's real answer, through the injectable http_get.

LEM (`LEM Web Server/web_app.py`, `api_get_corrections`) answers
`200 {"corrections": [{"test_name", "correction", "units"}], "methods": [...]}`.
An unknown machine is `200 {"corrections": [], "methods": []}` (checked against
the live server 2026-09-28), and an unreadable LabCore is a 502/503 JSON body
from `_labcore_unreadable` — never a 200 with an empty list.

The rules (design, "LEM corrections (2C)"): a 200 JSON answer is authoritative;
the machine is known iff `methods` is non-empty; a mapped name in `methods` but
not in `corrections` is 0.0 (LEM's own rule); a mapped name in neither is a
configuration error; units must be °C, C or empty. Anything that means "LEM was
not asked" is `unreachable`, and only that may fall back to the cache (≤ 24 h).

A cache entry belongs to one LEM machine under one map. Served for another —
after the uid is corrected, or the map edited — it would put one GC's offsets
on another GC's results, so any mismatch is "no cache".
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import corrections as C

UID = "bf8e64b59f12"
GC1 = {"id": "gc1", "lem_machine_uid": UID, "correction_map": None}
T0 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
T0_ISO = "2026-09-28T10:00:00+00:00"
LEM_NAMES = list(C.DEFAULT_LEM_CORRECTION_MAP)          # the five real names
PHASE1_NAMES = list(C.PHASE1_FILE_MAP)                   # eleven '* - D86'
ELEVEN = dict(GC1, correction_map=json.dumps(C.PHASE1_FILE_MAP))


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class FakeLem:
    """`(url, timeout) -> (status, content_type, body)`; records every call."""

    def __init__(self):
        self.calls = []
        self.answer = (200, "application/json", b"{}")
        self.raise_exc = None
        self.delay = 0.0

    def json(self, corrections=(), methods=(), status=200):
        body = {"corrections": [dict(test_name=n, correction=v, units=u)
                                for n, v, u in corrections],
                "methods": list(methods)}
        self.answer = (status, "application/json", json.dumps(body).encode())

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.delay:
            time.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.answer


@pytest.fixture
def lem():
    fake = FakeLem()
    # LEM knows the machine, reports its five real methods, holds five values.
    fake.json([(n, -1.0 - i, "°C") for i, n in enumerate(LEM_NAMES)], methods=LEM_NAMES)
    return fake


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store():
    return C.MemoryCacheStore()


def make(lem, store, clock, **kw):
    return C.LemProvider("http://lem.local:5000/", store, http_get=lem, clock=clock, **kw)


def raises(provider, kind, instrument=GC1, refresh=False):
    with pytest.raises(C.CorrectionsUnavailable) as info:
        (provider.refresh if refresh else provider.get)(instrument)
    assert info.value.kind == kind, info.value.reason
    return info.value


FIVE = {"IBP": -1.0, "10%": -2.0, "50%": -3.0, "90%": -4.0, "FBP": -5.0}


# ── the answer ───────────────────────────────────────────────────────────────

def test_the_default_map_reads_the_five_real_methods(lem, store, clock):
    got = make(lem, store, clock).get(GC1)
    assert got.source == "lem"
    assert got.fetched_at == T0_ISO
    assert got.stale_reason == ""
    assert got.values == FIVE
    assert list(got.values) == ["IBP", "10%", "50%", "90%", "FBP"]


def test_unmapped_cuts_are_not_invented(lem, store, clock):
    """The pipeline leaves them uncorrected; the record must not claim LEM said 0."""
    got = make(lem, store, clock).get(GC1)
    assert "5%" not in got.values and "95%" not in got.values


def test_all_eleven_cuts_with_an_eleven_name_map(lem, store, clock):
    lem.json([(n, -1.0 - i, "C") for i, n in enumerate(PHASE1_NAMES)], methods=PHASE1_NAMES)
    got = make(lem, store, clock).get(ELEVEN)
    assert list(got.values) == C.D86_CUTS
    assert got.values == {cut: -1.0 - i for i, cut in enumerate(C.D86_CUTS)}


def test_url_and_timeout(lem, store, clock):
    make(lem, store, clock, timeout=2.5).get(GC1)
    assert lem.calls == [(f"http://lem.local:5000/api/machines/{UID}/corrections", 2.5)]


def test_default_timeout_is_five_seconds(lem, store, clock):
    make(lem, store, clock).get(GC1)
    assert lem.calls[0][1] == 5.0


def test_uid_is_quoted(lem, store, clock):
    make(lem, store, clock).get(dict(GC1, lem_machine_uid="a/b c"))
    assert lem.calls[0][0].endswith("/api/machines/a%2Fb%20c/corrections")


def test_in_methods_not_in_corrections_is_explicit_zero(lem, store, clock):
    lem.json([(LEM_NAMES[0], -12.08, "°C"), (LEM_NAMES[4], -5.57, "C")], methods=LEM_NAMES)
    assert make(lem, store, clock).get(GC1).values == {
        "IBP": -12.08, "10%": 0.0, "50%": 0.0, "90%": 0.0, "FBP": -5.57}


def test_todays_real_answer_is_five_explicit_zeros(lem, store, clock):
    """Agilent GC 1 today: no corrections saved, five methods mapped."""
    lem.json([], methods=LEM_NAMES)
    assert make(lem, store, clock).get(GC1).values == {c: 0.0 for c in FIVE}


def test_in_corrections_but_missing_from_methods_still_counts(lem, store, clock):
    lem.json([(LEM_NAMES[0], 2.0, "C")], methods=LEM_NAMES[1:])
    assert make(lem, store, clock).get(GC1).values["IBP"] == 2.0


def test_unknown_machine_is_config(lem, store, clock):
    lem.json([], methods=[])
    err = raises(make(lem, store, clock), "config")
    assert UID in err.reason


def test_unknown_machine_never_uses_the_cache(lem, store, clock):
    p = make(lem, store, clock)
    p.get(GC1)
    lem.json([], methods=[])
    raises(p, "config", refresh=True)


def test_a_mapped_name_in_neither_is_config(lem, store, clock):
    """E.g. the phase-1 '* - D86' names against real LEM."""
    lem.json([], methods=LEM_NAMES)
    err = raises(make(lem, store, clock), "config", ELEVEN)
    assert "IBP - D86" in err.reason and "FBP - D86" in err.reason


def test_one_missing_name_is_enough_for_config(lem, store, clock):
    lem.json([], methods=LEM_NAMES[:-1])
    err = raises(make(lem, store, clock), "config")
    assert LEM_NAMES[-1] in err.reason


def test_bad_custom_map_is_config_before_asking_lem(lem, store, clock):
    raises(make(lem, store, clock), "config", dict(GC1, correction_map="[1]"))
    assert lem.calls == []


@pytest.mark.parametrize("uid", [None, "", "   "])
def test_no_machine_uid_is_config(lem, store, clock, uid):
    raises(make(lem, store, clock), "config", dict(GC1, lem_machine_uid=uid))
    assert lem.calls == []


@pytest.mark.parametrize("url", ["", "  ", None])
def test_no_lem_url_is_config_and_asks_nobody(lem, store, clock, url):
    raises(C.LemProvider(url, store, http_get=lem, clock=clock), "config")
    assert lem.calls == []


@pytest.mark.parametrize("units", ["°C", "C", "", "  °C "])
def test_accepted_units(lem, store, clock, units):
    lem.json([(LEM_NAMES[0], 1.0, units)], methods=LEM_NAMES)
    assert make(lem, store, clock).get(GC1).values["IBP"] == 1.0


@pytest.mark.parametrize("units", ["°F", "F", "K", "mg/kg", "degC"])
def test_other_units_are_config(lem, store, clock, units):
    lem.json([(LEM_NAMES[0], 1.0, units)], methods=LEM_NAMES)
    err = raises(make(lem, store, clock), "config")
    assert units in err.reason


def test_null_units_count_as_empty(lem, store, clock):
    body = {"corrections": [{"test_name": LEM_NAMES[0], "correction": 1.0, "units": None}],
            "methods": LEM_NAMES}
    lem.answer = (200, "application/json", json.dumps(body).encode())
    assert make(lem, store, clock).get(GC1).values["IBP"] == 1.0


def test_units_of_an_unmapped_test_do_not_matter(lem, store, clock):
    lem.json([("Sulfur - D5453", 0.5, "mg/kg")], methods=LEM_NAMES + ["Sulfur - D5453"])
    assert make(lem, store, clock).get(GC1).values["IBP"] == 0.0


@pytest.mark.parametrize("value", ["abc", None, "NaN", True])
def test_a_mapped_non_number_is_config(lem, store, clock, value):
    body = {"corrections": [{"test_name": LEM_NAMES[0], "correction": value, "units": "C"}],
            "methods": LEM_NAMES}
    lem.answer = (200, "application/json", json.dumps(body).encode())
    raises(make(lem, store, clock), "config")


@pytest.mark.parametrize("body", [
    [],
    {"corrections": []},
    {"methods": LEM_NAMES},
    {"corrections": {}, "methods": LEM_NAMES},
    {"corrections": [], "methods": "IBP"},
    {"corrections": ["IBP"], "methods": LEM_NAMES},
])
def test_json_of_the_wrong_shape_is_config(lem, store, clock, body):
    """A 200 JSON answer that isn't LEM's shape: LEM_URL points at something
    else. A person has to fix that; retrying won't."""
    lem.answer = (200, "application/json", json.dumps(body).encode())
    raises(make(lem, store, clock), "config")


# ── unreachable ─────────────────────────────────────────────────────────────

UNREACHABLE_ANSWERS = [
    (200, "text/html; charset=utf-8", b"<html><body>Sign in</body></html>"),
    (200, "application/json", b"<html>not json</html>"),
    (200, "text/html", b'{"corrections": [], "methods": []}'),
    (500, "text/html", b"Internal Server Error"),
    (502, "application/json", b'{"error": "LabCore refused", "retry": true, "labcore": "refused"}'),
    (503, "application/json", b'{"error": "LabCore did not answer", "retry": true, "labcore": "unavailable"}'),
    (504, "text/html", b"Gateway Timeout"),
    (429, "text/plain", b"slow down"),
    (401, "text/html", b"<html>Sign in</html>"),
    (403, "text/html", b"<html>Forbidden</html>"),
]


@pytest.mark.parametrize("answer", UNREACHABLE_ANSWERS)
def test_unreachable_answers(lem, store, clock, answer):
    lem.answer = answer
    raises(make(lem, store, clock), "unreachable")


@pytest.mark.parametrize("status", [401, 403])
def test_a_sign_in_wall_says_so_and_uses_the_cache(lem, store, clock, status):
    p = make(lem, store, clock)
    p.get(GC1)
    lem.answer = (status, "text/html", b"<html>Sign in</html>")
    clock.advance(hours=1)
    got = p.refresh(GC1)
    assert got.source == "cache"
    assert "sign-in" in got.stale_reason.lower() and str(status) in got.stale_reason


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), ConnectionRefusedError(61, "refused"),
                                 OSError("no route"), RuntimeError("anything else")])
def test_exceptions_are_unreachable(lem, store, clock, exc):
    lem.raise_exc = exc
    err = raises(make(lem, store, clock), "unreachable")
    assert "LEM" in err.reason


@pytest.mark.parametrize("status", [301, 400, 404, 405, 410])
def test_other_statuses_are_config(lem, store, clock, status):
    """A 404 is a wrong LEM_URL: waiting will not fix it, and it may not be
    answered from the cache."""
    lem.answer = (status, "text/html", b"nope")
    raises(make(lem, store, clock), "config")


def test_the_503_reason_carries_lems_message(lem, store, clock):
    lem.answer = UNREACHABLE_ANSWERS[5]
    err = raises(make(lem, store, clock), "unreachable")
    assert "503" in err.reason and "LabCore did not answer" in err.reason


# ── the cache ───────────────────────────────────────────────────────────────

def test_a_good_fetch_is_saved_with_its_machine_and_map(lem, store, clock):
    make(lem, store, clock).get(GC1)
    entry = store.load("gc1")
    assert entry["fetched_at"] == T0_ISO
    assert entry["values"] == FIVE
    assert entry["methods"] == LEM_NAMES
    assert entry["lem_machine_uid"] == UID
    assert entry["map_key"] == C.map_key(C.DEFAULT_LEM_CORRECTION_MAP)


def _seed(lem, store, clock):
    make(lem, store, clock).get(GC1)          # fetched at T0, saved
    lem.answer = UNREACHABLE_ANSWERS[5]


def test_cache_exactly_24h_old_is_used(lem, store, clock):
    _seed(lem, store, clock)
    clock.advance(hours=24)
    got = make(lem, store, clock).get(GC1)
    assert got.source == "cache"
    assert got.fetched_at == T0_ISO
    assert got.values == FIVE
    assert "503" in got.stale_reason


def test_cache_24h_and_a_second_is_not(lem, store, clock):
    _seed(lem, store, clock)
    clock.advance(hours=24, seconds=1)
    err = raises(make(lem, store, clock), "unreachable")
    assert "cache" in err.reason.lower()


def test_max_cache_age_is_configurable(lem, store, clock):
    _seed(lem, store, clock)
    clock.advance(hours=2)
    raises(make(lem, store, clock, max_cache_age=3600), "unreachable")


def test_no_cache_raises_unreachable(lem, store, clock):
    lem.answer = UNREACHABLE_ANSWERS[0]
    raises(make(lem, store, clock), "unreachable")


def test_a_timeout_falls_back_too(lem, store, clock):
    _seed(lem, store, clock)
    lem.raise_exc = TimeoutError()
    clock.advance(hours=1)
    assert make(lem, store, clock).get(GC1).source == "cache"


def test_a_config_error_never_falls_back(lem, store, clock):
    _seed(lem, store, clock)
    clock.advance(hours=1)
    lem.answer = (404, "text/html", b"")
    raises(make(lem, store, clock), "config")


def test_uid_corrected_while_lem_is_down_serves_no_stale_cache(lem, store, clock):
    """GC 1's offsets must never land on the machine the uid now names."""
    _seed(lem, store, clock)
    clock.advance(minutes=10)
    raises(make(lem, store, clock), "unreachable", dict(GC1, lem_machine_uid="3afa991a66e9"))


def test_map_edited_while_lem_is_down_serves_no_stale_cache(lem, store, clock):
    """Same cuts, different names: the cut set alone cannot tell."""
    _seed(lem, store, clock)
    clock.advance(minutes=10)
    swapped = {LEM_NAMES[0]: "FBP", LEM_NAMES[4]: "IBP", LEM_NAMES[1]: "10%",
               LEM_NAMES[2]: "50%", LEM_NAMES[3]: "90%"}
    raises(make(lem, store, clock), "unreachable", dict(GC1, correction_map=json.dumps(swapped)))


def test_restart_within_five_minutes_after_a_uid_change_refetches(lem, store, clock):
    make(lem, store, clock).get(GC1)
    clock.advance(minutes=1)
    other = dict(GC1, lem_machine_uid="3afa991a66e9")
    got = make(lem, store, clock).get(other)        # a restarted hub
    assert len(lem.calls) == 2 and lem.calls[1][0].endswith("/3afa991a66e9/corrections")
    assert got.fetched_at != T0_ISO


def test_restart_within_five_minutes_after_a_map_change_refetches(lem, store, clock):
    make(lem, store, clock).get(GC1)
    clock.advance(minutes=1)
    got = make(lem, store, clock).get(dict(GC1, correction_map=json.dumps(
        {LEM_NAMES[0]: "IBP"})))
    assert len(lem.calls) == 2 and got.values == {"IBP": -1.0}


def test_an_entry_without_machine_or_map_is_no_cache(lem, clock):
    class LegacyStore(C.MemoryCacheStore):
        def load(self, instrument_id):
            entry = super().load(instrument_id)
            if entry:
                entry.pop("lem_machine_uid")
                entry.pop("map_key")
            return entry
    s = LegacyStore()
    make(lem, s, clock).get(GC1)
    lem.answer = UNREACHABLE_ANSWERS[5]
    raises(make(lem, s, clock), "unreachable")


def test_a_cache_from_the_future_is_not_used(lem, store, clock):
    store.save("gc1", FIVE, LEM_NAMES, "2026-09-29T10:00:00+00:00",
               lem_machine_uid=UID, map_key=C.map_key(C.DEFAULT_LEM_CORRECTION_MAP))
    lem.answer = UNREACHABLE_ANSWERS[5]
    raises(make(lem, store, clock), "unreachable")


def test_offsets_are_compared_as_instants(lem, store, clock):
    """Stored with another UTC offset (e.g. written before a DST change) the
    same instant is still one hour old, not 'from the future'."""
    store.save("gc1", FIVE, LEM_NAMES, "2026-09-28T03:00:00-07:00",
               lem_machine_uid=UID, map_key=C.map_key(C.DEFAULT_LEM_CORRECTION_MAP))
    clock.advance(hours=1)
    lem.answer = UNREACHABLE_ANSWERS[5]
    assert make(lem, store, clock).get(GC1).source == "cache"


def test_an_unreadable_cache_entry_is_no_cache(lem, store, clock):
    store.save("gc1", FIVE, LEM_NAMES, "not a time",
               lem_machine_uid=UID, map_key=C.map_key(C.DEFAULT_LEM_CORRECTION_MAP))
    lem.answer = UNREACHABLE_ANSWERS[5]
    raises(make(lem, store, clock), "unreachable")


class BrokenStore:
    def load(self, instrument_id):
        raise RuntimeError("db locked")

    def save(self, *a, **kw):
        raise RuntimeError("db locked")


def test_store_load_failing_is_no_cache(lem, clock):
    lem.answer = UNREACHABLE_ANSWERS[5]
    raises(make(lem, BrokenStore(), clock), "unreachable")


def test_store_save_failing_does_not_fail_a_good_fetch(lem, clock):
    assert make(lem, BrokenStore(), clock).get(GC1).source == "lem"


def test_memory_store_returns_copies(store):
    store.save("gc1", {"IBP": 1.0}, ["x"], T0_ISO, lem_machine_uid=UID, map_key="k")
    entry = store.load("gc1")
    entry["values"]["IBP"] = 99.0
    entry["methods"].append("y")
    assert store.load("gc1") == {"values": {"IBP": 1.0}, "methods": ["x"],
                                 "fetched_at": T0_ISO, "lem_machine_uid": UID,
                                 "map_key": "k"}
    assert store.load("gc2") is None


# ── freshness (at most one fetch per 5 minutes) ─────────────────────────────

def test_within_five_minutes_no_second_fetch(lem, store, clock):
    p = make(lem, store, clock)
    first = p.get(GC1)
    clock.advance(seconds=299)
    assert p.get(GC1) == first
    assert len(lem.calls) == 1


def test_at_five_minutes_it_fetches_again(lem, store, clock):
    p = make(lem, store, clock)
    p.get(GC1)
    clock.advance(seconds=300)
    lem.json([(LEM_NAMES[0], 7.0, "C")], methods=LEM_NAMES)
    got = p.get(GC1)
    assert len(lem.calls) == 2
    assert got.values["IBP"] == 7.0 and got.fetched_at == "2026-09-28T10:05:00+00:00"


def test_fresh_seconds_is_configurable(lem, store, clock):
    p = make(lem, store, clock, fresh_seconds=10)
    p.get(GC1)
    clock.advance(seconds=10)
    p.get(GC1)
    assert len(lem.calls) == 2


def test_store_freshness_survives_a_restart(lem, store, clock):
    make(lem, store, clock).get(GC1)
    clock.advance(seconds=120)
    got = make(lem, store, clock).get(GC1)     # a new provider: a restarted hub
    assert len(lem.calls) == 1
    assert got.source == "lem" and got.fetched_at == T0_ISO


def test_a_changed_map_bypasses_freshness(lem, store, clock):
    p = make(lem, store, clock)
    p.get(GC1)
    assert p.get(dict(GC1, correction_map=json.dumps({LEM_NAMES[0]: "IBP"}))).values == \
        {"IBP": -1.0}
    assert len(lem.calls) == 2


def test_a_changed_uid_bypasses_freshness(lem, store, clock):
    p = make(lem, store, clock)
    p.get(GC1)
    p.get(dict(GC1, lem_machine_uid="other"))
    assert len(lem.calls) == 2


def test_instruments_are_separate(lem, store, clock):
    p = make(lem, store, clock)
    p.get(GC1)
    p.get({"id": "gc2", "lem_machine_uid": "3afa991a66e9", "correction_map": None})
    assert len(lem.calls) == 2
    assert store.load("gc2") is not None


def test_an_outage_is_remembered_for_five_minutes(lem, store, clock):
    """A backlog of samples during an outage must not each wait out the 5 s
    timeout; the cache still answers for every one of them."""
    _seed(lem, store, clock)
    clock.advance(seconds=301)
    p = make(lem, store, clock)
    assert p.get(GC1).source == "cache"
    for _ in range(5):
        clock.advance(seconds=10)
        assert p.get(GC1).source == "cache"
    assert len(lem.calls) == 2           # the seed + one attempt
    clock.advance(seconds=300)
    p.get(GC1)
    assert len(lem.calls) == 3


def test_a_remembered_outage_still_ages_the_cache(lem, store, clock):
    _seed(lem, store, clock)
    clock.advance(hours=24)
    p = make(lem, store, clock)
    assert p.get(GC1).source == "cache"
    clock.advance(seconds=1)
    raises(p, "unreachable")


def test_a_config_error_is_remembered_for_five_minutes(lem, store, clock):
    lem.json([], methods=[])
    p = make(lem, store, clock)
    raises(p, "config")
    raises(p, "config")
    assert len(lem.calls) == 1


def test_refresh_always_fetches(lem, store, clock):
    p = make(lem, store, clock)
    p.get(GC1)
    lem.json([(LEM_NAMES[0], 3.0, "C")], methods=LEM_NAMES)
    got = p.refresh(GC1)
    assert got.values["IBP"] == 3.0 and len(lem.calls) == 2
    assert got.source == "lem" and got.stale_reason == ""
    assert p.get(GC1).values["IBP"] == 3.0 and len(lem.calls) == 2


def test_refresh_bypasses_a_remembered_outage(lem, store, clock):
    _seed(lem, store, clock)
    clock.advance(seconds=301)
    p = make(lem, store, clock)
    p.get(GC1)
    lem.json([(LEM_NAMES[0], 4.0, "C")], methods=LEM_NAMES)
    assert p.refresh(GC1).source == "lem"


def test_refresh_when_unreachable_says_it_fell_back(lem, store, clock):
    """'Refresh from LEM' must not look like it worked when it didn't."""
    _seed(lem, store, clock)
    got = make(lem, store, clock).refresh(GC1)
    assert got.source == "cache"
    assert "LabCore did not answer" in got.stale_reason


def test_one_fetch_for_concurrent_callers(lem, store, clock):
    """Two worker threads asking at once share one fetch (per-instrument lock)."""
    lem.delay = 0.2
    p = make(lem, store, clock)
    results = []
    threads = [threading.Thread(target=lambda: results.append(p.get(GC1))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 4 and len(lem.calls) == 1


# ── change detection ───────────────────────────────────────────────────────

def test_changed_since_nothing_fetched_is_false(lem, store, clock):
    p = make(lem, store, clock)
    assert p.changed_since(GC1, C.Corrections("lem", "x", {"IBP": 1.0})) is False


def test_changed_since_same_values_is_false(lem, store, clock):
    p = make(lem, store, clock)
    used = p.get(GC1)
    clock.advance(minutes=10)
    p.get(GC1)
    assert p.changed_since(GC1, used) is False


def test_changed_since_a_different_value_is_true(lem, store, clock):
    p = make(lem, store, clock)
    used = p.get(GC1)
    lem.json([(LEM_NAMES[0], 0.25, "C")], methods=LEM_NAMES)
    p.refresh(GC1)
    assert p.changed_since(GC1, used) is True


def test_changed_since_file_eleven_vs_lem_five_same_values_is_false(lem, store, clock):
    """The 2A1 -> 2C switch: a sample corrected from the file (eleven cuts,
    six of them 0.0) is not 'changed' by LEM holding the same five."""
    p = make(lem, store, clock)
    p.get(GC1)
    used = C.Corrections("file", "2026-09-27T10:00:00+00:00",
                         {cut: FIVE.get(cut, 0.0) for cut in C.D86_CUTS})
    assert p.changed_since(GC1, used) is False


def test_changed_since_reads_the_latest_fetch_from_the_store(lem, store, clock):
    """Another provider (the worker vs the page) may have done the fetch."""
    used = make(lem, store, clock).get(GC1)
    store.save("gc1", dict(used.values, IBP=5.0), LEM_NAMES, "2026-09-28T11:00:00+00:00",
               lem_machine_uid=UID, map_key=C.map_key(C.DEFAULT_LEM_CORRECTION_MAP))
    assert make(lem, store, clock).changed_since(GC1, used) is True


def test_changed_since_ignores_a_fetch_for_another_machine(lem, store, clock):
    used = make(lem, store, clock).get(GC1)
    store.save("gc1", dict(used.values, IBP=5.0), LEM_NAMES, "2026-09-28T11:00:00+00:00",
               lem_machine_uid="someone-else", map_key=C.map_key(C.DEFAULT_LEM_CORRECTION_MAP))
    assert make(lem, store, clock).changed_since(GC1, used) is False


def test_changed_since_cache_and_lem_with_equal_values_is_false(lem, store, clock):
    p = make(lem, store, clock)
    fetched = p.get(GC1)
    used = C.Corrections("cache", "2026-09-27T10:00:00+00:00", dict(fetched.values))
    assert p.changed_since(GC1, used) is False
