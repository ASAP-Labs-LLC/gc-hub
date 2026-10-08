"""v8.0.1: Chrome start-up for QBench uploads. A driver that no longer
starts a session (Chrome updated since the hub started) is retried once with
Selenium Manager, every attempt gets a fresh profile folder, and the reason
the operator sees is chromedriver's whole message, not just its first line."""
import os
from unittest import mock

import pytest

qpu = pytest.importorskip("qbench_pdf_uploader")
if not qpu._SELENIUM_OK:
    pytest.skip("selenium not installed", allow_module_level=True)
from selenium.common.exceptions import SessionNotCreatedException


def test_the_reason_keeps_the_lines_after_session_not_created():
    exc = SessionNotCreatedException(
        "session not created\nfrom chrome not reachable\nStacktrace:\n#0 0x55 <unknown>")
    assert qpu.chrome_start_text(exc) == "session not created; from chrome not reachable"


def test_a_version_mismatch_reads_whole():
    exc = SessionNotCreatedException(
        "session not created: This version of ChromeDriver only supports Chrome version 140\n"
        "Current browser version is 141.0.7390.55 with binary path C:\\chrome.exe")
    text = qpu.chrome_start_text(exc)
    assert "only supports Chrome version 140" in text and "Current browser version is 141" in text


def test_a_stale_driver_falls_back_to_selenium_manager(tmp_path, monkeypatch):
    old = tmp_path / "chromedriver.exe"
    old.write_text("x")
    monkeypatch.setattr(qpu, "_driver_path", str(old))
    monkeypatch.setattr(qpu.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(qpu, "find_chrome", lambda: "C:/chrome.exe")
    calls = []
    good = mock.Mock(capabilities={"browserVersion": "141", "chrome": {"chromedriverVersion": "141.0 (x)"}})

    def chrome(service=None, options=None):
        calls.append((service, options))
        if service is not None:
            raise SessionNotCreatedException("session not created\nfrom chrome not reachable")
        return good
    monkeypatch.setattr(qpu.webdriver, "Chrome", chrome)
    assert qpu._make_driver() is good
    assert len(calls) == 2 and calls[1][0] is None
    assert qpu._driver_path is None                      # the stale driver is not reused
    dirs = [a.split("=", 1)[1] for _, o in calls for a in o.arguments if a.startswith("--user-data-dir=")]
    assert len(dirs) == 2 and dirs[0] != dirs[1]
    assert all(os.path.isdir(d) and d.startswith(str(tmp_path)) for d in dirs)


def test_both_attempts_failing_raise_the_last_error(monkeypatch, tmp_path):
    monkeypatch.setattr(qpu, "_driver_path", None)
    monkeypatch.setattr(qpu.tempfile, "gettempdir", lambda: str(tmp_path))

    def chrome(service=None, options=None):
        raise SessionNotCreatedException("session not created\nChrome failed to start: crashed")
    monkeypatch.setattr(qpu.webdriver, "Chrome", chrome)
    with pytest.raises(SessionNotCreatedException):
        qpu._make_driver()


def test_the_production_message_reads_whole_without_the_docs_link():
    exc = SessionNotCreatedException(
        "session not created\nfrom unknown error: cannot find Chrome binary; For documentation on this "
        "error, please visit: https://www.selenium.dev/documentation/webdriver/troubleshooting/errors"
        "#sessionnotcreatedexception\nStacktrace:\nSymbols not available.")
    assert qpu.chrome_start_text(exc) == "session not created; from unknown error: cannot find Chrome binary"


def test_no_chrome_found_goes_straight_to_selenium_manager(monkeypatch, tmp_path):
    """ASAPSV1, 2026-10-08: "cannot find Chrome binary". Without a Chrome the
    start-up driver is not tried; Selenium Manager fetches Chrome for Testing."""
    old = tmp_path / "chromedriver.exe"
    old.write_text("x")
    monkeypatch.setattr(qpu, "_driver_path", str(old))
    monkeypatch.setattr(qpu.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(qpu, "find_chrome", lambda: None)
    calls = []
    good = mock.Mock(capabilities={})

    def chrome(service=None, options=None):
        calls.append((service, options.binary_location))
        return good
    monkeypatch.setattr(qpu.webdriver, "Chrome", chrome)
    assert qpu._make_driver() is good
    assert calls == [(None, "")]


def test_find_chrome_prefers_the_setting_then_any_users_install(monkeypatch, tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_text("x")
    assert qpu.find_chrome({"QBENCH_CHROME": str(exe)}) == str(exe)
    assert qpu.find_chrome({"QBENCH_CHROME": str(tmp_path / "missing.exe")}) is None
    monkeypatch.setattr(qpu.sys, "platform", "win32")
    user = tmp_path / "Users" / "lab" / "AppData" / "Local" / "Google" / "Chrome" / "Application"
    user.mkdir(parents=True)
    (user / "chrome.exe").write_text("x")
    assert qpu.find_chrome({"SYSTEMDRIVE": str(tmp_path)}) == str(user / "chrome.exe")
    machine = tmp_path / "PF" / "Google" / "Chrome" / "Application"
    machine.mkdir(parents=True)
    (machine / "chrome.exe").write_text("x")
    assert qpu.find_chrome({"SYSTEMDRIVE": str(tmp_path), "PROGRAMFILES": str(tmp_path / "PF")}) \
        == str(machine / "chrome.exe")
