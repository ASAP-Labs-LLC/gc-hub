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
"""
import json
from datetime import datetime, timedelta

import pytest

import corrections as C

UID = "bf8e64b59f12"
GC1 = {"id": "gc1", "lem_machine_uid": UID, "correction_map": None}
T0 = datetime(2026, 9, 28, 10, 0, 0)
ALL_TESTS = list(C.DEFAULT_CORRECTION_MAP)


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

    def json(self, corrections=(), methods=(), status=200):
        body = {"corrections": [dict(test_name=n, correction=v, units=u)
                                for n, v, u in corrections],
                "methods": list(methods)}
        self.answer = (status, "application/json", json.dumps(body).encode())

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.answer


@pytest.fixture
def lem():
    fake = FakeLem()
    # Every test starts from "LEM knows the machine and holds 11 real values".
    fake.json([(n, -1.0 - i, "°C") for i, n in enumerate(ALL_TESTS)],
              methods=ALL_TESTS)
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


# ── the answer ───────────────────────────────────────────────────────────────

def test_all_eleven_cuts(lem, store, clock):
    got = make(lem, store, clock).get(GC1)
    assert got.source == "lem"
    assert got.fetched_at == "2026-09-28T10:00:00"
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
    lem.json([("IBP - D86", -12.08, "°C"), ("FBP - D86", -5.57, "C")], methods=ALL_TESTS)
    got = make(lem, store, clock).get(GC1).values
    assert got["IBP"] == -12.08 and got["FBP"] == -5.57
    assert {c: v for c, v in got.items() if c not in ("IBP", "FBP")} == \
        {c: 0.0 for c in C.D86_CUTS if c not in ("IBP", "FBP")}
    assert len(got) == 11


def test_a_saved_correction_counts_as_known_even_if_unmapped(lem, store, clock):
    """LEM adds every name carrying a correction to `methods`; a correction
    for a name the bench no longer maps is still one LEM would apply."""
    lem.json([(n, 1.0, "") for n in ALL_TESTS], methods=ALL_TESTS)
    assert set(make(lem, store, clock).get(GC1).values.values()) == {1.0}


def test_in_corrections_but_missing_from_methods_still_counts(lem, store, clock):
    lem.json([("IBP - D86", 2.0, "C")], methods=[n for n in ALL_TESTS if n != "IBP - D86"])
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
    """Today's real Agilent GC 1 answer: LEM's methods are the long names, so
    the default '* - D86' map matches none of them."""
    lem.json([], methods=[
        "ASTM D2887/D86 - Distillation in Petroleum Products, IBP",
        "ASTM D2887/D86 - Distillation in Petroleum Products, FBP"])
    err = raises(make(lem, store, clock), "config")
    assert "IBP - D86" in err.reason and "FBP - D86" in err.reason


def test_one_missing_name_is_enough_for_config(lem, store, clock):
    lem.json([], methods=ALL_TESTS[:-1])
    err = raises(make(lem, store, clock), "config")
    assert "FBP - D86" in err.reason


def test_custom_map_json_string(lem, store, clock):
    long = "ASTM D2887/D86 - Distillation in Petroleum Products, {}"
    names = {long.format("IBP"): "IBP", long.format("10% Recovery"): "10%",
             long.format("50% Recovery"): "50%", long.format("90% Recovery"): "90%",
             long.format("FBP"): "FBP"}
    lem.json([(long.format("IBP"), -12.08, "°C")], methods=list(names))
    inst = dict(GC1, correction_map=json.dumps(names))
    got = make(lem, store, clock).get(inst)
    assert got.values == {"IBP": -12.08, "10%": 0.0, "50%": 0.0, "90%": 0.0, "FBP": 0.0}


def test_bad_custom_map_is_config_before_asking_lem(lem, store, clock):
    raises(make(lem, store, clock), "config", dict(GC1, correction_map="[1]"))
    assert lem.calls == []


@pytest.mark.parametrize("uid", [None, "", "   "])
def test_no_machine_uid_is_config(lem, store, clock, uid):
    raises(make(lem, store, clock), "config", dict(GC1, lem_machine_uid=uid))
    assert lem.calls == []


@pytest.mark.parametrize("units", ["°C", "C", "", "  °C "])
def test_accepted_units(lem, store, clock, units):
    lem.json([("IBP - D86", 1.0, units)], methods=ALL_TESTS)
    assert make(lem, store, clock).get(GC1).values["IBP"] == 1.0


@pytest.mark.parametrize("units", ["°F", "F", "K", "mg/kg", "degC"])
def test_other_units_are_config(lem, store, clock, units):
    lem.json([("IBP - D86", 1.0, units)], methods=ALL_TESTS)
    err = raises(make(lem, store, clock), "config")
    assert units in err.reason


def test_null_units_count_as_empty(lem, store, clock):
    body = {"corrections": [{"test_name": "IBP - D86", "correction": 1.0, "units": None}],
            "methods": ALL_TESTS}
    lem.answer = (200, "application/json", json.dumps(body).encode())
    assert make(lem, store, clock).get(GC1).values["IBP"] == 1.0


def test_units_of_an_unmapped_test_do_not_matter(lem, store, clock):
    lem.json([("Sulfur - D5453", 0.5, "mg/kg")], methods=ALL_TESTS + ["Sulfur - D5453"])
    assert make(lem, store, clock).get(GC1).values["IBP"] == 0.0


@pytest.mark.parametrize("value", ["abc", None, "NaN", True])
def test_a_mapped_non_number_is_config(lem, store, clock, value):
    body = {"corrections": [{"test_name": "IBP - D86", "correction": value, "units": "C"}],
            "methods": ALL_TESTS}
    lem.answer = (200, "application/json", json.dumps(body).encode())
    raises(make(lem, store, clock), "config")


@pytest.mark.parametrize("body", [
    [],
    {"corrections": []},
    {"methods": ALL_TESTS},
    {"corrections": {}, "methods": ALL_TESTS},
    {"corrections": [], "methods": "IBP - D86"},
    {"corrections": ["IBP - D86"], "methods": ALL_TESTS},
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
]


@pytest.mark.parametrize("answer", UNREACHABLE_ANSWERS)
def test_unreachable_answers(lem, store, clock, answer):
    lem.answer = answer
    raises(make(lem, store, clock), "unreachable")


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), ConnectionRefusedError(61, "refused"),
                                 OSError("no route"), RuntimeError("anything else")])
def test_exceptions_are_unreachable(lem, store, clock, exc):
    lem.raise_exc = exc
    err = raises(make(lem, store, clock), "unreachable")
    assert "LEM" in err.reason


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 405])
def test_other_statuses_are_config(lem, store, clock, status):
    """A 404 is a wrong LEM_URL; a 401/403 is an auth wall in front of LEM.
    Neither heals by waiting, and neither may be answered from the cache."""
    lem.answer = (status, "text/html", b"nope")
    raises(make(lem, store, clock), "config")


def test_the_503_reason_carries_lems_message(lem, store, clock):
    lem.answer = UNREACHABLE_ANSWERS[5]
    err = raises(make(lem, store, clock), "unreachable")
    assert "503" in err.reason and "LabCore did not answer" in err.reason


# ── the cache ───────────────────────────────────────────────────────────────

def test_a_good_fetch_is_saved(lem, store, clock):
    make(lem, store, clock).get(GC1)
    entry = store.load("gc1")
    assert entry["fetched_at"] == "2026-09-28T10:00:00"
    assert entry["values"]["IBP"] == -1.0 and len(entry["values"]) == 11
    assert entry["methods"] == ALL_TESTS


def _seed(lem, store, clock):
    make(lem, store, clock).get(GC1)          # fetched at T0, saved
    lem.answer = UNREACHABLE_ANSWERS[5]


def test_cache_exactly_24h_old_is_used(lem, store, clock):
    _seed(lem, store, clock)
    clock.advance(hours=24)
    got = make(lem, store, clock).get(GC1)
    assert got.source == "cache"
    assert got.fetched_at == "2026-09-28T10:00:00"
    assert got.values["IBP"] == -1.0 and len(got.values) == 11


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


def test_a_cache_for_another_cut_set_is_not_used(lem, store, clock):
    _seed(lem, store, clock)
    clock.advance(hours=1)
    inst = dict(GC1, correction_map=json.dumps({"IBP - D86": "IBP"}))
    raises(make(lem, store, clock), "unreachable", inst)


def test_a_cache_from_the_future_is_not_used(lem, store, clock):
    store.save("gc1", {c: 0.0 for c in C.D86_CUTS}, ALL_TESTS, "2026-09-29T10:00:00")
    lem.answer = UNREACHABLE_ANSWERS[5]
    raises(make(lem, store, clock), "unreachable")


def test_an_unreadable_cache_entry_is_no_cache(lem, store, clock):
    store.save("gc1", {c: 0.0 for c in C.D86_CUTS}, ALL_TESTS, "not a time")
    lem.answer = UNREACHABLE_ANSWERS[5]
    raises(make(lem, store, clock), "unreachable")


class BrokenStore:
    def load(self, instrument_id):
        raise RuntimeError("db locked")

    def save(self, instrument_id, values, methods, fetched_at):
        raise RuntimeError("db locked")


def test_store_load_failing_is_no_cache(lem, clock):
    lem.answer = UNREACHABLE_ANSWERS[5]
    raises(make(lem, BrokenStore(), clock), "unreachable")


def test_store_save_failing_does_not_fail_a_good_fetch(lem, clock):
    assert make(lem, BrokenStore(), clock).get(GC1).source == "lem"


def test_memory_store_returns_copies(store):
    store.save("gc1", {"IBP": 1.0}, ["IBP - D86"], "2026-09-28T10:00:00")
    entry = store.load("gc1")
    entry["values"]["IBP"] = 99.0
    entry["methods"].append("x")
    assert store.load("gc1") == {"values": {"IBP": 1.0}, "methods": ["IBP - D86"],
                                 "fetched_at": "2026-09-28T10:00:00"}
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
    lem.json([("IBP - D86", 7.0, "C")], methods=ALL_TESTS)
    got = p.get(GC1)
    assert len(lem.calls) == 2
    assert got.values["IBP"] == 7.0 and got.fetched_at == "2026-09-28T10:05:00"


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
    assert got.source == "lem" and got.fetched_at == "2026-09-28T10:00:00"


def test_a_changed_map_bypasses_freshness(lem, store, clock):
    p = make(lem, store, clock)
    p.get(GC1)
    inst = dict(GC1, correction_map=json.dumps({"IBP - D86": "IBP"}))
    assert p.get(inst).values == {"IBP": -1.0}
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
    lem.json([("IBP - D86", 3.0, "C")], methods=ALL_TESTS)
    got = p.refresh(GC1)
    assert got.values["IBP"] == 3.0 and len(lem.calls) == 2
    assert p.get(GC1).values["IBP"] == 3.0 and len(lem.calls) == 2


def test_refresh_bypasses_a_remembered_outage(lem, store, clock):
    _seed(lem, store, clock)
    clock.advance(seconds=301)
    p = make(lem, store, clock)
    p.get(GC1)
    lem.json([("IBP - D86", 4.0, "C")], methods=ALL_TESTS)
    assert p.refresh(GC1).source == "lem"


def test_refresh_when_unreachable_still_falls_back(lem, store, clock):
    _seed(lem, store, clock)
    assert make(lem, store, clock).refresh(GC1).source == "cache"


# ── change detection ───────────────────────────────────────────────────────

def test_changed_since_nothing_fetched_is_false(lem, store, clock):
    p = make(lem, store, clock)
    assert p.changed_since("gc1", C.Corrections("lem", "x", {"IBP": 1.0})) is False


def test_changed_since_same_values_is_false(lem, store, clock):
    p = make(lem, store, clock)
    used = p.get(GC1)
    clock.advance(minutes=10)
    p.get(GC1)
    assert p.changed_since("gc1", used) is False


def test_changed_since_a_different_value_is_true(lem, store, clock):
    p = make(lem, store, clock)
    used = p.get(GC1)
    lem.json([("IBP - D86", 0.25, "C")], methods=ALL_TESTS)
    p.refresh(GC1)
    assert p.changed_since("gc1", used) is True


def test_changed_since_a_different_cut_set_is_true(lem, store, clock):
    p = make(lem, store, clock)
    p.get(GC1)
    assert p.changed_since("gc1", C.Corrections("file", "x", {"IBP": -1.0})) is True


def test_changed_since_reads_the_latest_fetch_from_the_store(lem, store, clock):
    """Another provider (the worker vs the page) may have done the fetch."""
    used = make(lem, store, clock).get(GC1)
    store.save("gc1", dict(used.values, IBP=5.0), ALL_TESTS, "2026-09-28T11:00:00")
    assert make(lem, store, clock).changed_since("gc1", used) is True


def test_changed_since_cache_and_lem_with_equal_values_is_false(lem, store, clock):
    p = make(lem, store, clock)
    fetched = p.get(GC1)
    used = C.Corrections("cache", "2026-09-27T10:00:00", dict(fetched.values))
    assert p.changed_since("gc1", used) is False


@pytest.mark.parametrize("url", ["", "  ", None])
def test_no_lem_url_is_config_and_asks_nobody(lem, store, clock, url):
    p = C.LemProvider(url, store, http_get=lem, clock=clock)
    raises(p, "config")
    assert lem.calls == []
