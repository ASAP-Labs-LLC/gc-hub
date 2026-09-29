"""labcore_auth.py: sign-in against LabCore's ``POST /api/login``, always
against a local stub (``tests/labcore_stub.py``), never the real service.

Rev 2 classification: only a JSON 4xx (other than 429) is a failed sign-in
(``None``). A network error, a 5xx, a 429, ``cf-mitigated``, a 403 whose body
isn't JSON (Cloudflare's ``error code: 1010``), or a 200 that isn't JSON or
has no ``username`` raise ``LabCoreUnavailable``.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import labcore_auth  # noqa: E402
from labcore_stub import LabCoreStub  # noqa: E402


@pytest.fixture()
def stub():
    with LabCoreStub() as s:
        yield s


def test_password_sign_in_returns_the_canonical_name(stub):
    assert labcore_auth.authenticate_user("RYAN C", "labpass-1", base_url=stub.url) == "Ryan C"
    headers, body = stub.requests[-1]
    assert body == {"username": "RYAN C", "password": "labpass-1"}
    assert headers["User-Agent"].startswith("gc-hub/")
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"


def test_wrong_password_is_none(stub):
    assert labcore_auth.authenticate_user("ryan c", "nope", base_url=stub.url) is None


def test_blank_fields_never_reach_labcore(stub):
    assert labcore_auth.authenticate_user("", "x", base_url=stub.url) is None
    assert labcore_auth.authenticate_user("ryan c", "", base_url=stub.url) is None
    assert labcore_auth.authenticate_card("  ", base_url=stub.url) is None
    assert stub.requests == []


def test_card_is_sent_as_username_and_password(stub):
    assert labcore_auth.authenticate_card(" CARD-0042 ", base_url=stub.url) == "Ryan C"
    assert stub.requests[-1][1] == {"username": "CARD-0042", "password": "CARD-0042"}
    assert labcore_auth.authenticate_card("CARD-9999", base_url=stub.url) is None


@pytest.mark.parametrize("mode", ["500", "429", "cf-1010", "cf-challenge", "html200",
                                  "no-username"])
def test_unavailable_classes(stub, mode):
    stub.mode = mode
    with pytest.raises(labcore_auth.LabCoreUnavailable):
        labcore_auth.authenticate_user("ryan c", "labpass-1", base_url=stub.url)


def test_network_error_is_unavailable():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(labcore_auth.LabCoreUnavailable):
        labcore_auth.authenticate_user("ryan c", "x", base_url=f"http://127.0.0.1:{port}")


def test_timeout_is_unavailable(stub):
    stub.mode, stub.delay = "slow", 1.0
    with pytest.raises(labcore_auth.LabCoreUnavailable):
        labcore_auth.authenticate_user("ryan c", "labpass-1", base_url=stub.url, timeout=0.2)


def test_the_password_is_not_in_the_exception_text(stub):
    stub.mode = "500"
    with pytest.raises(labcore_auth.LabCoreUnavailable) as ei:
        labcore_auth.authenticate_user("ryan c", "labpass-1", base_url=stub.url)
    assert "labpass-1" not in str(ei.value)


def test_base_url_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("LABCORE_URL", raising=False)
    assert labcore_auth.base_url() == "https://labvision.asaplabs.net"
    monkeypatch.setenv("LABCORE_URL", "http://127.0.0.1:9/ ")
    assert labcore_auth.base_url() == "http://127.0.0.1:9"


def test_env_url_is_used_when_no_base_url_is_given(stub, monkeypatch):
    monkeypatch.setenv("LABCORE_URL", stub.url)
    assert labcore_auth.authenticate_user("jane doe", "labpass-2") == "Jane Doe"


def test_a_huge_name_is_refused(stub):
    stub.accounts["big"] = ("pw", "N" * 500)
    with pytest.raises(labcore_auth.LabCoreUnavailable):
        labcore_auth.authenticate_user("big", "pw", base_url=stub.url)


def test_stdlib_only():
    src = (TESTS.parent / "labcore_auth.py").read_text(encoding="utf-8")
    assert "import requests" not in src and "from requests" not in src
