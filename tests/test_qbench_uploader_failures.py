"""v6.0.0: why a QBench upload failed reaches the operator and app.log.

``attach_pdf_to_sample`` keeps its contract (True / False, and
``LoginFailedError`` for the caller's credential re-prompt), and every False
now leaves one plain line, ``last_failure()``, that the hub shows in the
report queue. Everything it says goes through ``logging`` (app.log), never
``print`` (stdout is not kept on ASAPSV1).

No browser and no QBench: the Selenium and API seams are patched.
"""
from __future__ import annotations

import ast
import logging
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "qbench_pdf_uploader.py").read_text(encoding="utf-8")


def test_the_uploader_never_prints():
    """stdout is not app.log: every message goes through the module's logger."""
    tree = ast.parse(SRC)
    prints = [n.lineno for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"]
    assert prints == [], f"print() at lines {prints}"
    assert "traceback.print_exc" not in SRC


q = pytest.importorskip("qbench_pdf_uploader")
if not getattr(q, "_SELENIUM_OK", False):     # pragma: no cover
    pytest.skip("selenium is not installed", allow_module_level=True)
from selenium.common.exceptions import TimeoutException, WebDriverException  # noqa: E402


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "40304_analysis.pdf"
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    return p


def _attach(pdf, **kw):
    seen = []
    kw.setdefault("username", "lab@example.com")
    kw.setdefault("password", "pw")
    ok = q.attach_pdf_to_sample("40304", pdf, progress_callback=seen.append, **kw)
    return ok, seen


def test_failure_line_is_one_plain_line():
    assert q.failure_line("Message: session not created\nStacktrace:\n0x1") == \
        "Message: session not created"
    assert q.failure_line("") == ""
    assert len(q.failure_line("y" * 1000)) == 240


def test_missing_pdf(tmp_path):
    ok, _ = _attach(tmp_path / "nope.pdf")
    assert ok is False
    assert q.last_failure().startswith("The report PDF was not found")


def test_api_key_missing_is_named(pdf, caplog):
    """No QBench API key under the account the hub runs as: say so (it used to
    end as 'No QBench sample found', then 'Upload returned false')."""
    import qbench_secrets
    err = qbench_secrets.QBenchSecretMissing(
        "QBench client_id is not configured. Enter it in the app under Settings > QBench API")
    with mock.patch("qbench_client.QBenchAPIClient", side_effect=err), \
            caplog.at_level(logging.INFO, logger="qbench_pdf_uploader"):
        ok, seen = _attach(pdf)
    assert ok is False
    assert q.last_failure().startswith("QBench API: QBench client_id is not configured")
    assert "Settings > QBench API" in q.last_failure()
    assert any("not configured" in r.getMessage() and r.levelno >= logging.WARNING
               for r in caplog.records)


def test_no_sample_for_the_lab_id(pdf):
    client = mock.Mock()
    client.fetch_samples_by_lab_id.return_value = []
    with mock.patch("qbench_client.QBenchAPIClient", return_value=client):
        ok, _ = _attach(pdf)
    assert ok is False
    assert q.last_failure() == "QBench has no sample with lab ID '40304'."


def test_api_timeout(pdf, monkeypatch):
    monkeypatch.setattr(q, "API_LOOKUP_TIMEOUT_S", 0.2)
    gate = threading.Event()

    def slow(**_kw):
        gate.wait(5)
        return mock.Mock()
    with mock.patch("qbench_client.QBenchAPIClient", side_effect=slow):
        ok, _ = _attach(pdf)
    gate.set()
    assert ok is False
    assert "did not answer" in q.last_failure()


def test_unreadable_credentials_file(pdf, monkeypatch, tmp_path):
    monkeypatch.setattr(q, "_api_lookup", lambda *a, **k: 123)
    monkeypatch.setattr(q, "CREDENTIALS_FILE", str(tmp_path / "missing" / "qbenchlogin.txt"))
    ok, _ = _attach(pdf, username=None, password=None)
    assert ok is False
    assert q.last_failure().startswith("Can't read the saved QBench sign-in file")
    assert "qbenchlogin.txt" in q.last_failure()


def test_empty_credentials_file(pdf, monkeypatch, tmp_path):
    f = tmp_path / "qbenchlogin.txt"
    f.write_text("\n", encoding="utf-8")
    monkeypatch.setattr(q, "_api_lookup", lambda *a, **k: 123)
    monkeypatch.setattr(q, "CREDENTIALS_FILE", str(f))
    ok, _ = _attach(pdf, username=None, password=None)
    assert ok is False
    assert "is empty" in q.last_failure()


def test_credentials_file_with_a_bom(tmp_path):
    """Notepad's 'UTF-8 with BOM' must not put the BOM in the username."""
    f = tmp_path / "qbenchlogin.txt"
    f.write_bytes("﻿lab@example.com\r\nsecret\r\n".encode("utf-8"))
    assert q.read_credentials_file(str(f)) == ("lab@example.com", "secret")


def test_chrome_that_cannot_start(pdf, monkeypatch, caplog):
    monkeypatch.setattr(q, "_api_lookup", lambda *a, **k: 123)
    monkeypatch.setattr(q, "_make_driver", mock.Mock(side_effect=WebDriverException(
        "session not created: This version of ChromeDriver only supports Chrome version 114\n"
        "Current browser version is 129.0.6668.59\nStacktrace:\n0x00 0x01")))
    monkeypatch.setattr(q, "_verify_attachment_fresh", lambda *a, **k: False)
    with caplog.at_level(logging.INFO, logger="qbench_pdf_uploader"):
        ok, _ = _attach(pdf)
    assert ok is False
    line = q.last_failure()
    assert line.startswith("Chrome could not start: ")
    assert "only supports Chrome version 114" in line
    assert "\n" not in line and "Stacktrace" not in line
    # the whole message, stack included, is in the log
    assert any("Stacktrace" in (r.getMessage() + (r.exc_text or "")) for r in caplog.records)


def test_a_page_that_never_loads_names_the_step(pdf, monkeypatch):
    """A TimeoutException has an empty message: it used to read 'Chrome
    process likely crashed'. Say which step timed out."""
    driver = mock.MagicMock()
    driver.current_url = "https://asaplabs.qbench.net/sample?id=123"
    driver.find_element.side_effect = q.NoSuchElementException("no login button")
    monkeypatch.setattr(q, "_api_lookup", lambda *a, **k: 123)
    monkeypatch.setattr(q, "_make_driver", lambda **k: driver)
    monkeypatch.setattr(q, "_verify_attachment_fresh", lambda *a, **k: False)
    monkeypatch.setattr(q.time, "sleep", lambda s: None)

    class Never:
        def __init__(self, *a, **k):
            pass

        def until(self, *_a, **_k):
            raise TimeoutException()
    monkeypatch.setattr(q, "WebDriverWait", Never)
    ok, _ = _attach(pdf)
    assert ok is False
    assert q.last_failure().startswith("QBench did not answer in time")
    assert "Uploading 40304_analysis.pdf" in q.last_failure()


def test_success_clears_the_last_failure(pdf, monkeypatch):
    q._set_failure("old")
    driver = mock.MagicMock()
    driver.current_url = "https://asaplabs.qbench.net/sample?id=123#attachments"
    driver.page_source = "40304_analysis"
    driver.find_element.side_effect = q.NoSuchElementException("x")
    monkeypatch.setattr(q, "_api_lookup", lambda *a, **k: 123)
    monkeypatch.setattr(q, "_make_driver", lambda **k: driver)
    monkeypatch.setattr(q.time, "sleep", lambda s: None)
    wait = mock.MagicMock()
    monkeypatch.setattr(q, "WebDriverWait", lambda *a, **k: wait)
    ok, _ = _attach(pdf)
    assert ok is True
    assert q.last_failure() == ""


def test_login_failure_still_propagates(pdf, monkeypatch):
    """app.py's credential re-prompt depends on LoginFailedError."""
    driver = mock.MagicMock()
    driver.current_url = "https://asaplabs.qbench.net/login"
    monkeypatch.setattr(q, "_api_lookup", lambda *a, **k: 123)
    monkeypatch.setattr(q, "_make_driver", lambda **k: driver)
    monkeypatch.setattr(q.time, "sleep", lambda s: None)
    monkeypatch.setattr(q, "_login", mock.Mock(side_effect=q.LoginFailedError("Bad credentials: x")))
    with pytest.raises(q.LoginFailedError):
        _attach(pdf)


def test_the_reason_is_per_thread(pdf, tmp_path):
    """The upload thread reads its own reason, never another thread's."""
    q._set_failure("")
    t = threading.Thread(target=lambda: q.attach_pdf_to_sample("1", tmp_path / "x.pdf"))
    t.start()
    t.join()
    assert q.last_failure() == ""
