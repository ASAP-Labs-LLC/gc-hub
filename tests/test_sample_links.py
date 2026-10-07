"""sample_links.py: sendable sample links (spec "Sendable sample links (v3.1)").

``/lab/<lab_id>`` resolves a lab ID to its newest run (the latest
``injection_dt``, preferring final runs, across instruments; exact match
first, then case-insensitive; the URL is decoded once, by the server),
``/samples/<id>[/compare|/data]`` open one exact run, and ``GET
/api/lab/<lab_id>`` is the JSON resolver. Every route needs a session; a
signed-out link lands back on itself after sign-in.

In process, on a bare Flask app wired the way app.py wires it (gate, the
blueprint, a stand-in view for the Samples page), never ``import app``.
v6.0.0: the classic page's old links redirect to the same view here. LabCore is ``tests/labcore_stub.py``.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")

from flask import Flask  # noqa: E402

import admin_auth  # noqa: E402
import netctx  # noqa: E402
import sample_links  # noqa: E402
import store  # noqa: E402
import web_auth  # noqa: E402
from labcore_stub import LabCoreStub  # noqa: E402

SAMPLES = "<p>the Samples page</p>"
LAN = {"REMOTE_ADDR": "10.0.0.25"}


def make_app():
    app = Flask("sample_links_test", template_folder=str(ROOT / "templates"),
                static_folder=str(ROOT / "static"))
    app.register_blueprint(admin_auth.bp)
    app.register_blueprint(web_auth.bp)
    app.register_blueprint(sample_links.bp)
    app.before_request(web_auth.require_https)
    app.before_request(web_auth.gate)
    app.context_processor(web_auth.template_context)

    # a stand-in for app.py's Samples page
    @app.route("/samples")
    def samples_list_page():
        return SAMPLES

    return app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("GC_DATA_DIR", str(data))
    with LabCoreStub() as stub:
        monkeypatch.setenv("LABCORE_URL", stub.url)
        admin_auth.reset_throttle()
        web_auth.reset()
        netctx._proxy_cache.clear()
        db = admin_auth.hub_db()
        store.instruments.upsert({"id": "gc1", "name": "GC-1"}, db=db)
        store.instruments.upsert({"id": "gc2", "name": "GC-2"}, db=db)
        app = make_app()
        yield {"client": app.test_client(), "db": db, "app": app}
        web_auth.reset()
        admin_auth.reset_throttle()


_n = [0]


def add(db, lab_id, dt, instrument="gc1", status="final") -> int:
    _n[0] += 1
    return store.samples.insert_received(instrument, lab_id, dt, "cdf",
                                         cdf_sha256=f"sha-links-{_n[0]}",
                                         cdf_path=f"cdf/{_n[0]}.CDF", method_name="SIMDISB.M",
                                         status=status, db=db)


def signed_in(env) -> str:
    """A live session row + its cookie on the test client; returns the token."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="microseconds")
    store.web_sessions.add(hashlib.sha256(token.encode()).hexdigest(), name="Test Operator",
                           method="password", ip="127.0.0.1", user_agent="tests",
                           expires_at=expires, db=env["db"])
    env["client"].set_cookie("gc_session", token)
    return token


def get(env, path):
    return env["client"].get(path, environ_base=LAN)


def api(env, path):
    r = get(env, path)
    return r.status_code, r.get_json()


# ── the resolver ────────────────────────────────────────────────────────────

def test_a_single_run_resolves_to_itself(env):
    signed_in(env)
    sid = add(env["db"], "40329", "2026-09-28 10:00:00")
    code, body = api(env, "/api/lab/40329")
    assert code == 200, body
    assert body["sample_id"] == sid and body["lab_id"] == "40329"
    assert body["runs"] == [{"sample_id": sid, "lab_id": "40329", "instrument": "gc1",
                             "instrument_name": "GC-1", "injection_dt": "2026-09-28 10:00:00",
                             "status": "final"}]


def test_runs_on_two_instruments_open_the_newest_and_list_both(env):
    signed_in(env)
    db = env["db"]
    older = add(db, "40329", "2026-09-27 09:00:00", "gc1")
    newer = add(db, "40329", "2026-09-28 15:30:00", "gc2")
    add(db, "403290", "2026-09-29 08:00:00", "gc1")          # a longer ID: not a match
    code, body = api(env, "/api/lab/40329")
    assert code == 200
    assert body["sample_id"] == newer
    assert [r["sample_id"] for r in body["runs"]] == [newer, older]    # newest first
    assert [r["instrument_name"] for r in body["runs"]] == ["GC-2", "GC-1"]


def test_a_final_run_is_preferred_over_a_newer_held_one(env):
    signed_in(env)
    db = env["db"]
    final = add(db, "40330", "2026-09-27 09:00:00", "gc1")
    held = add(db, "40330", "2026-09-29 09:00:00", "gc2", status="awaiting_calibration")
    code, body = api(env, "/api/lab/40330")
    assert code == 200 and body["sample_id"] == final
    assert {r["sample_id"] for r in body["runs"]} == {final, held}


def test_with_no_final_run_the_newest_is_opened(env):
    signed_in(env)
    db = env["db"]
    add(db, "40331", "2026-09-27 09:00:00", status="error")
    newest = add(db, "40331", "2026-09-28 09:00:00", status="pending_corrections")
    assert api(env, "/api/lab/40331")[1]["sample_id"] == newest


def test_a_rerun_suffix_and_its_case_variants(env):
    signed_in(env)
    sid = add(env["db"], "40318-RERUN-2", "2026-09-28 11:00:00")
    for raw in ("40318-RERUN-2", "40318-rerun-2", "40318-Rerun-2", "40318%2DRERUN%2D2",
                "40318%2drerun%2d2"):
        code, body = api(env, f"/api/lab/{raw}")
        assert code == 200, raw
        assert body["sample_id"] == sid and body["lab_id"] == "40318-RERUN-2", raw


def test_an_exact_match_wins_over_a_case_insensitive_one(env):
    signed_in(env)
    db = env["db"]
    upper = add(db, "AB12", "2026-09-27 09:00:00")
    lower = add(db, "ab12", "2026-09-28 09:00:00")
    assert api(env, "/api/lab/AB12")[1]["sample_id"] == upper
    assert [r["sample_id"] for r in api(env, "/api/lab/AB12")[1]["runs"]] == [upper]
    assert api(env, "/api/lab/ab12")[1]["sample_id"] == lower
    # neither spelled exactly: both, newest first
    code, body = api(env, "/api/lab/Ab12")
    assert code == 200 and body["sample_id"] == lower
    assert [r["sample_id"] for r in body["runs"]] == [lower, upper]


def test_the_lab_id_is_decoded_once_only(env):
    signed_in(env)
    db = env["db"]
    literal = add(db, "A%41", "2026-09-28 09:00:00")
    add(db, "AA", "2026-09-28 10:00:00")
    code, body = api(env, "/api/lab/A%2541")         # → "A%41", not "AA"
    assert code == 200 and body["sample_id"] == literal
    add(db, "40340", "2026-09-28 09:00:00")
    assert api(env, "/api/lab/40340%2520")[0] == 404   # → "40340%20", never "40340 "


def test_like_wildcards_are_literal(env):
    signed_in(env)
    add(env["db"], "40350", "2026-09-28 09:00:00")
    assert api(env, "/api/lab/4035_")[0] == 404
    assert api(env, "/api/lab/%25")[0] == 404


def test_an_unknown_lab_id_is_a_404(env):
    signed_in(env)
    code, body = api(env, "/api/lab/99999")
    assert code == 404
    assert body["lab_id"] == "99999" and "99999" in body["error"]


# ── the pages ───────────────────────────────────────────────────────────────

def test_the_lab_page_lands_on_the_samples_page(env):
    """v5.0.0: /lab/<lab_id> redirects to /samples/<its newest run>."""
    signed_in(env)
    add(env["db"], "40329", "2026-09-28 10:00:00")
    newest = add(env["db"], "40329", "2026-09-29 10:00:00")
    r = get(env, "/lab/40329")
    assert r.status_code == 302 and r.headers["Location"] == f"/samples/{newest}"
    r = get(env, "/lab/40329%2DX")      # unknown
    assert r.status_code == 404


def test_an_old_classic_lab_link_redirects_to_the_lab_link(env):
    """v6.0.0: the classic page is gone; its bookmarks keep working."""
    signed_in(env)
    add(env["db"], "40329", "2026-09-28 10:00:00")
    r = get(env, "/classic/lab/40329")
    assert r.status_code == 302 and r.headers["Location"] == "/lab/40329"
    # re-quoted (every character but letters, digits and _.-~): the target
    # is always one path segment on this hub
    r = get(env, "/classic/lab/a%20b%3Fc%23d")
    assert r.status_code == 302 and r.headers["Location"] == "/lab/a%20b%3Fc%23d"
    # an unknown lab ID: the friendly page, one hop later
    r = get(env, "/classic/lab/99999")
    assert r.status_code == 302 and get(env, r.headers["Location"]).status_code == 404


def test_an_unknown_lab_id_gets_a_friendly_page(env):
    signed_in(env)
    r = get(env, "/lab/99999")
    assert r.status_code == 404
    html = r.get_data(as_text=True)
    assert "No GC result for lab ID" in html and "99999" in html and "yet" in html
    assert 'href="/?q=99999"' in html                  # a link to search
    assert 'id="app-version"' in html                  # the version badge
    assert 'data-testid="link-not-found"' in html


def test_the_not_found_page_escapes_the_lab_id(env):
    signed_in(env)
    r = get(env, "/lab/%3Cb%3Ex%22")
    assert r.status_code == 404
    html = r.get_data(as_text=True)
    assert "<b>x" not in html and "&lt;b&gt;x" in html
    assert 'href="/?q=%3Cb%3Ex%22"' in html


@pytest.mark.parametrize("suffix", ["", "/compare", "/compare?standard=Diesel", "/data"])
def test_sample_pages_open_the_samples_page(env, suffix):
    signed_in(env)
    sid = add(env["db"], "40329", "2026-09-28 10:00:00")
    r = get(env, f"/samples/{sid}{suffix}")
    assert r.status_code == 200 and r.get_data(as_text=True) == SAMPLES


@pytest.mark.parametrize("suffix", ["", "/compare", "/compare?standard=Diesel%20B", "/data"])
def test_old_classic_sample_links_redirect_to_the_samples_page(env, suffix):
    """v6.0.0: same run, same view, query kept (``?standard=``)."""
    signed_in(env)
    sid = add(env["db"], "40329", "2026-09-28 10:00:00")
    r = get(env, f"/classic/samples/{sid}{suffix}")
    assert r.status_code == 302 and r.headers["Location"] == f"/samples/{sid}{suffix}"
    assert get(env, r.headers["Location"]).get_data(as_text=True) == SAMPLES


@pytest.mark.parametrize("suffix", ["", "/compare", "/data"])
def test_an_unknown_sample_gets_the_friendly_page(env, suffix):
    signed_in(env)
    r = get(env, f"/samples/424242{suffix}")
    assert r.status_code == 404
    html = r.get_data(as_text=True)
    assert "No GC result" in html and "424242" in html


# ── sign-in ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/lab/40329", "/lab/40318-RERUN-2", "/samples/7",
                                  "/samples/7/compare?standard=Diesel", "/samples/7/data"])
def test_safe_next_accepts_the_link_paths(path):
    assert web_auth.safe_next(path) == path


@pytest.mark.parametrize("path", ["/lab/40329", "/api/lab/40329", "/samples/7",
                                  "/samples/7/compare", "/samples/7/data", "/samples", "/classic",
                                  "/classic/lab/40329", "/classic/samples/7/data"])
def test_every_link_route_needs_a_session(path):
    assert web_auth.route_class(path) == "session"


def test_signed_out_the_api_is_401(env):
    add(env["db"], "40329", "2026-09-28 10:00:00")
    r = get(env, "/api/lab/40329")
    assert r.status_code == 401 and r.get_json()["login_required"] is True


def test_a_signed_out_link_lands_back_on_it_after_sign_in(env):
    add(env["db"], "40329", "2026-09-28 10:00:00")
    c = env["client"]
    r = get(env, "/lab/40329")
    assert r.status_code == 302 and r.headers["Location"] == "/login?next=/lab/40329"
    r = c.get("/login?next=/lab/40329", environ_base=LAN)
    assert r.status_code == 200 and "/lab/40329" in r.get_data(as_text=True)
    r = c.post("/api/login", data=json.dumps({"username": "ryan c", "password": "labpass-1",
                                              "next": "/lab/40329"}),
               headers={"Content-Type": "application/json"}, environ_base=LAN)
    assert r.status_code == 200 and r.get_json()["next"] == "/lab/40329"
    r = get(env, "/lab/40329")
    assert r.status_code == 302 and r.headers["Location"].startswith("/samples/")


@pytest.mark.parametrize("raw", ["40%3F1", "40%231", "40%2541", "40%25", "a%5Cb", "a%20b",
                                 "%C3%A9t%C3%A9"])
def test_a_signed_out_link_keeps_its_escapes_through_sign_in(env, raw):
    """next is the path re-quoted: the browser, sent to next after sign-in,
    requests exactly the same /lab/<raw> (?, #, % and backslash survive)."""
    from urllib.parse import parse_qs, unquote, urlsplit
    r = get(env, "/lab/" + raw)
    assert r.status_code == 302
    nxt = parse_qs(urlsplit(r.headers["Location"]).query)["next"][0]
    assert web_auth.safe_next(nxt) == nxt
    after = urlsplit(nxt)
    assert not after.query and not after.fragment
    assert unquote(after.path) == unquote("/lab/" + raw)


def test_next_keeps_the_query_and_the_guards(env):
    r = get(env, "/samples/7/compare?standard=Diesel%20B")
    assert r.status_code == 302
    assert r.headers["Location"] == "/login?next=/samples/7/compare%3Fstandard%3DDiesel%2520B"
    r = get(env, "/instruments?x=1")
    assert r.headers["Location"] == "/login?next=/instruments%3Fx%3D1"
    assert web_auth.safe_next("/%5Cevil.example") == "/%5Cevil.example"   # a relative path
    for bad in ("//evil.example", "/\\evil.example", "/api/lab/1", "https://evil.example"):
        assert web_auth.safe_next(bad) == "/"


# ── the copied link's address ───────────────────────────────────────────────

def _session(env, host, token):
    r = env["app"].test_client(use_cookies=False).get(
        "/api/session", environ_base=LAN, headers={"Host": host, "Cookie": f"gc_session={token}"})
    assert r.status_code == 200
    return r.get_json()


def test_the_session_carries_the_link_url_even_over_the_lan(env):
    token = signed_in(env)
    assert _session(env, "192.168.1.20:5560", token)["link_url"] == "https://gc.asaplabs.net"


def test_an_admin_set_public_hub_url_is_the_link_url(env):
    token = signed_in(env)
    store.settings_kv.set("hub_url", "https://gc.example.org", db=env["db"])
    assert _session(env, "asapsv1:5560", token)["link_url"] == "https://gc.example.org"


@pytest.mark.parametrize("lan", ["http://asapsv1:5560", "http://192.168.1.20:5560",
                                 "https://asapsv1.local", "http://[fd00::5]:5560",
                                 "https://10.0.0.5"])
def test_a_lan_hub_url_falls_back_to_the_public_address(env, lan):
    """A link is meant to be sent: a LAN-only hub URL would not open elsewhere."""
    token = signed_in(env)
    store.settings_kv.set("hub_url", lan, db=env["db"])
    assert _session(env, "asapsv1:5560", token)["link_url"] == "https://gc.asaplabs.net"


# ── lookups the critic found ────────────────────────────────────────────────

def test_an_old_exact_lab_id_is_not_crowded_out_by_newer_near_misses(env):
    signed_in(env)
    db = env["db"]
    sid = add(db, "4032", "2020-01-01 00:00:00")
    with store.connection(db) as conn:
        with store.write_txn(conn):
            conn.executemany(
                "INSERT INTO samples(instrument_id, lab_id, injection_dt, injection_dt_source, "
                "status, received_at) VALUES ('gc1', ?, ?, 'cdf', 'final', '2026-01-01')",
                [(f"4032{i % 10}",
                  f"2026-01-01 {i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d}")
                 for i in range(5001)])
    code, body = api(env, "/api/lab/4032")
    assert code == 200 and body["sample_id"] == sid and len(body["runs"]) == 1


def test_non_ascii_lab_ids_match_exactly_only(env):
    """SQLite's NOCASE folds A-Z only: é and É are different (documented)."""
    signed_in(env)
    sid = add(env["db"], "ÉTÉ-1", "2026-09-28 10:00:00")
    assert api(env, "/api/lab/%C3%89T%C3%89-1")[1]["sample_id"] == sid
    assert api(env, "/api/lab/%C3%89t%C3%89-1")[1]["sample_id"] == sid    # t: ASCII
    assert api(env, "/api/lab/%C3%A9t%C3%A9-1")[0] == 404                 # é: not


@pytest.mark.parametrize("path", ["/samples/99999999999999999999",
                                  "/samples/99999999999999999999/compare",
                                  "/samples/99999999999999999999/data",
                                  "/samples/9223372036854775808", "/samples/0"])
def test_huge_or_zero_sample_ids_get_the_friendly_page(env, path):
    signed_in(env)
    r = get(env, path)
    assert r.status_code == 404
    assert 'data-testid="link-not-found"' in r.get_data(as_text=True)


@pytest.mark.parametrize("path", ["/api/lab/%00", "/lab/%00", "/lab/" + "9" * 201])
def test_odd_lab_ids_are_not_found(env, path):
    signed_in(env)
    assert get(env, path).status_code == 404
