"""D10: ``lem_url`` is saved through /api/settings with the admin password
only, and must be a bare http(s)://host[:port] (a bad value is a 400 and
nothing is written)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

pytest.importorskip("flask")

from bootapp import TEST_ADMIN_PASSWORD, booted, get, post, setup_admin  # noqa: E402

PW = {"password": TEST_ADMIN_PASSWORD}


@pytest.fixture(scope="module")
def hub():
    with tempfile.TemporaryDirectory() as t:
        with booted(Path(t)) as (port, _proc, data, _home):
            setup_admin(port, data)
            yield port


def _conf(port):
    return get(port, "/api/settings")[1]


def test_default_is_served(hub):
    assert _conf(hub)["lem_url"] == "https://lem.asaplabs.net"


def test_needs_the_admin_password(hub):
    before = _conf(hub)
    code, _body = post(hub, "/api/settings", dict(before, lem_url="http://lem2:8080"))
    assert code == 403
    assert _conf(hub)["lem_url"] == before["lem_url"]


@pytest.mark.parametrize("value", [
    "lem.asaplabs.net", "https://lem.asaplabs.net/api", "file:///etc/passwd",
    "https://user:pw@lem", "", 5,
])
def test_bad_values_are_refused(hub, value):
    before = _conf(hub)
    code, body = post(hub, "/api/settings", dict(before, **PW, lem_url=value))
    assert code == 400, body
    assert "lem_url" in body["error"]
    assert _conf(hub)["lem_url"] == before["lem_url"]


def test_good_value_is_saved(hub):
    conf = _conf(hub)
    code, body = post(hub, "/api/settings", dict(conf, **PW, lem_url="http://lem2:8080"))
    assert code == 200, body
    assert _conf(hub)["lem_url"] == "http://lem2:8080"
