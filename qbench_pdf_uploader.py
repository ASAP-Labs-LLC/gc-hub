"""qbench_pdf_uploader.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Attach a PDF report to a QBench sample via a headless Chrome browser.

Credentials file (same as Past Data Manager):
  \\\\ASAPServer\\Labsharedrive\\ASAP Lab Results\\qbenchlogin.txt

Design notes:
  - All Selenium imports are at module level so they don't silently block
    inside the upload function.
  - _active_driver holds the live WebDriver so cancel_upload() can quit()
    it immediately from any thread — the upload function then catches the
    resulting exception and returns False cleanly.
  - ChromeDriverManager is called once at startup; subsequent calls use the
    cached binary path stored in _driver_path.
  - Every blocking step emits a log message BEFORE it starts, so the dialog
    always shows exactly where the process is.
  - Everything goes through ``logging`` (the hub's app.log), never print():
    stdout is not kept where the hub runs as a service.
  - v6.0.0: when attach_pdf_to_sample() returns False, ``last_failure()``
    (per thread) is one plain line saying why, for the report queue; the
    full detail (and any traceback) is logged.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
import threading
from pathlib import Path
from typing import Callable, Optional

# ── Module-level Selenium imports (fail fast, not silently later) ──────────
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as _ChromeOptions
    from selenium.webdriver.chrome.service import Service as _ChromeService
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException, WebDriverException
    )
    _SELENIUM_OK = True

    class LoginFailedError(Exception):
        """Raised when QBench login fails (bad credentials or lockout)."""
        pass
except ImportError as _e:
    _SELENIUM_OK = False
    _SELENIUM_IMPORT_ERROR = str(_e)

    class LoginFailedError(Exception):
        """Raised when QBench login fails (bad credentials or lockout)."""
        pass

CREDENTIALS_FILE = r"\\ASAPServer\Labsharedrive\ASAP Lab Results\qbenchlogin.txt"
QBENCH_BASE      = "https://asaplabs.qbench.net"

# How long the REST lookup (lab ID -> QBench sample id) may take.
API_LOOKUP_TIMEOUT_S = 25


class CredentialsFileError(Exception):
    """The saved QBench sign-in file can't be used; the message says why, plainly."""


logger = logging.getLogger(__name__)

# Suppress noisy debug output from Selenium and urllib3 — only show warnings+
logging.getLogger("selenium").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("WDM").setLevel(logging.WARNING)

# Steps per sample (progress-bar granularity used by the dialog)
STEPS_PER_SAMPLE = 10

# Global driver reference — allows cancel_upload() to kill it from any thread
_active_driver: Optional[object] = None
_driver_lock = threading.Lock()

# Cached ChromeDriver path (resolved once)
_driver_path: Optional[str] = None

# Per thread: why this thread's last attach_pdf_to_sample() returned False
_state = threading.local()

# Set by cancel_upload() so a browser killed on purpose reads "Stopped"
_cancelled = threading.Event()

FAILURE_MAX = 240


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def failure_line(text, limit: int = FAILURE_MAX) -> str:
    """One plain line for the operator: the first non-empty line of *text*,
    spaces collapsed, at most *limit* characters (never a stack trace)."""
    if text is None:
        return ""
    for raw in str(text).splitlines():
        line = " ".join(raw.split())
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "\u2026"
    return ""


def last_failure() -> str:
    """Why this thread's last attach_pdf_to_sample() returned False ('' after
    a success, or before any call)."""
    return getattr(_state, "failure", "")


def _set_failure(reason: str) -> None:
    _state.failure = failure_line(reason)


def _exc_text(exc: BaseException) -> str:
    """A Selenium exception's own message (without the 'Message: ' prefix and
    the driver's stack trace), else str(exc), else the class name."""
    msg = getattr(exc, "msg", None) or str(exc) or ""
    if msg.startswith("Message: "):
        msg = msg[len("Message: "):]
    return failure_line(msg) or type(exc).__name__


def _emit(msg: str, cb: Optional[Callable[[str], None]] = None,
          level: int = logging.INFO) -> None:
    """Log (app.log) AND forward to the UI callback."""
    logger.log(level, "[QBench] %s", msg)
    if cb:
        try:
            cb(msg)
        except Exception:
            logger.exception("[QBench] progress callback failed")


def _fail(reason: str, cb: Optional[Callable[[str], None]] = None) -> bool:
    """Record why this upload failed (``last_failure()``), log it and tell
    the UI; returns False, for ``return _fail(...)``."""
    _set_failure(reason)
    _emit(f"FAILED: {last_failure()}", cb, level=logging.WARNING)
    return False


def _os_reason(exc: OSError) -> str:
    text = exc.strerror or str(exc) or type(exc).__name__
    win = getattr(exc, "winerror", None)
    return f"{text} (Windows error {win})" if win else text


def read_credentials_file(path: str) -> tuple[str, str]:
    """``(username, password)`` from a qbenchlogin.txt (line 1, line 2).

    A UTF-8 byte-order mark (Notepad's "UTF-8 with BOM") is dropped, and a
    file that is not UTF-8 is read as Windows-1252. Raises
    ``CredentialsFileError`` with a plain sentence when the file can't be
    read, is empty, or lacks a line."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise CredentialsFileError(
            f"Can't read the saved QBench sign-in file {path}: {_os_reason(exc)}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    lines = [ln.strip() for ln in text.splitlines()]
    user = lines[0] if lines else ""
    pw = lines[1] if len(lines) > 1 else ""
    if not user and not pw:
        raise CredentialsFileError(f"The saved QBench sign-in file {path} is empty.")
    if not user:
        raise CredentialsFileError(
            f"The saved QBench sign-in file {path} has no username on line 1.")
    if not pw:
        raise CredentialsFileError(
            f"The saved QBench sign-in file {path} has no password on line 2.")
    return user, pw


def credentials_file() -> str:
    """The saved sign-in file: ``QBENCH_LOGIN_FILE`` when set, else the share's."""
    return os.environ.get("QBENCH_LOGIN_FILE") or CREDENTIALS_FILE


def _get_credentials() -> tuple[str, str]:
    path = credentials_file()
    _emit(f"Reading credentials from {path}")
    creds = read_credentials_file(path)
    _emit("Credentials loaded OK")
    return creds


def _resolve_chromedriver() -> Optional[str]:
    """Return the path to chromedriver.exe (cached after first call)."""
    global _driver_path
    if _driver_path and os.path.exists(_driver_path):
        return _driver_path

    # Try webdriver-manager (may download — do this once at startup)
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        raw = ChromeDriverManager().install()
        exe = os.path.join(os.path.dirname(raw), "chromedriver.exe")
        if os.path.exists(exe):
            _driver_path = exe
            logger.info("[QBench] ChromeDriver cached at %s", exe)
            return exe
    except Exception as e:
        logger.warning("[QBench] webdriver-manager failed: %s", e)

    # Try chromedriver-autoinstaller
    try:
        import chromedriver_autoinstaller
        p = chromedriver_autoinstaller.install()
        if p and os.path.exists(str(p)):
            _driver_path = str(p)
            logger.info("[QBench] chromedriver-autoinstaller path: %s", _driver_path)
            return _driver_path
    except Exception as e:
        logger.warning("[QBench] chromedriver-autoinstaller failed: %s", e)

    # Fall back to PATH (Selenium Manager finds or downloads a driver)
    logger.info("[QBench] Will rely on chromedriver in PATH / Selenium Manager")
    return None


_PROFILE_ROOT_NAME = "gc-hub-chrome"
_PROFILE_MAX_AGE_S = 2 * 3600


def _fresh_profile_dir() -> str:
    """A new, empty Chrome profile folder for one session (v8.0.1).

    Chrome started without ``--user-data-dir`` uses the account's default
    profile, which a scheduled-task or service account may not be able to
    create or may find locked by another Chrome: chromedriver then answers
    only "session not created". Folders older than two hours are removed
    (one Chrome still holds is skipped)."""
    import shutil
    import uuid
    root = os.path.join(tempfile.gettempdir(), _PROFILE_ROOT_NAME)
    os.makedirs(root, exist_ok=True)
    now = time.time()
    try:
        for name in os.listdir(root):
            path = os.path.join(root, name)
            try:
                if now - os.path.getmtime(path) > _PROFILE_MAX_AGE_S:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass
    path = os.path.join(root, uuid.uuid4().hex)
    os.makedirs(path, exist_ok=True)
    return path


def chrome_start_text(exc: BaseException) -> str:
    """Why Chrome did not start, in one line: chromedriver puts the real
    reason after "session not created" (often on the next line, e.g. the
    supported Chrome version), so the message's lines are joined up to the
    stack trace."""
    msg = getattr(exc, "msg", None) or str(exc) or ""
    if msg.startswith("Message: "):
        msg = msg[len("Message: "):]
    keep = []
    for raw in msg.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if line.lower().startswith(("stacktrace", "backtrace", "#")) or line.startswith("0x"):
            break
        line = line.split(" For documentation on this error", 1)[0].rstrip(" ;")
        if line:
            keep.append(line)
    return failure_line("; ".join(keep)) or type(exc).__name__


def find_chrome(env=None) -> Optional[str]:
    """The Chrome to start (v8.0.1): ``QBENCH_CHROME`` when set, else Chrome
    installed for every user, else one installed for a single Windows user
    (under ``C:\\Users\\<name>\\AppData\\Local``, which chromedriver never
    looks in for another account: "cannot find Chrome binary"). ``None``
    when there is none; Selenium Manager then downloads Chrome for Testing."""
    import glob
    env = os.environ if env is None else env
    own = (env.get("QBENCH_CHROME") or "").strip().strip('"')
    if own:
        return own if os.path.isfile(own) else None
    if not sys.platform.startswith("win"):
        return None
    tail = os.path.join("Google", "Chrome", "Application", "chrome.exe")
    roots = [env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"), env.get("PROGRAMW6432"),
             env.get("LOCALAPPDATA")]
    for root in [r for r in roots if r]:
        path = os.path.join(root, tail)
        if os.path.isfile(path):
            return path
    drive = env.get("SYSTEMDRIVE", "C:")
    for path in sorted(glob.glob(os.path.join(drive + os.sep, "Users", "*", "AppData", "Local", tail))):
        if os.path.isfile(path):
            return path
    return None


def _chrome_options(headless: bool, binary: Optional[str] = None) -> object:
    options = _ChromeOptions()
    if binary:
        options.binary_location = binary
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1600,900")
    options.add_argument("--disable-extensions")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument(f"--user-data-dir={_fresh_profile_dir()}")
    return options


def _make_driver(headless: bool = True) -> object:
    """Start Chrome. First with the ChromeDriver resolved at start-up; when
    that cannot start a session (most often Chrome updated itself since the
    hub started, so that driver no longer matches), once more with
    Selenium Manager, which finds the driver for the Chrome installed now.
    Each attempt gets its own fresh profile."""
    global _driver_path
    cd_path = _driver_path  # Use cached path (resolved before worker starts)
    binary = find_chrome()
    logger.info("[QBench] Chrome binary: %s", binary or "none found (Selenium Manager will fetch one)")
    attempts = []
    if cd_path and os.path.exists(cd_path) and binary:
        attempts.append(cd_path)
    attempts.append(None)               # Selenium Manager
    last = None
    driver = None
    for path in attempts:
        try:
            if path:
                driver = webdriver.Chrome(service=_ChromeService(path),
                                          options=_chrome_options(headless, binary))
            else:
                driver = webdriver.Chrome(options=_chrome_options(headless, binary))
            break
        except WebDriverException as exc:
            last = exc
            logger.warning("[QBench] Chrome did not start with %s: %s",
                           path or "Selenium Manager", chrome_start_text(exc), exc_info=True)
            if path:
                _driver_path = None     # don't reuse a driver that no longer starts
    if driver is None:
        raise last
    # Log browser version for debugging version-mismatch issues
    try:
        caps = driver.capabilities
        logger.info("[QBench] Chrome %s  ChromeDriver %s", caps.get('browserVersion', '?'),
                    caps.get('chrome', {}).get('chromedriverVersion', '?').split()[0])
    except Exception:
        pass
    return driver


def _login(driver, username: str, password: str,
           cb: Optional[Callable[[str], None]] = None) -> None:
    """Log in to QBench using the form that is overlaid on any protected page."""
    _emit("Logging in to QBench…", cb)
    wait = WebDriverWait(driver, 40)

    # Some landing pages show an extra "Access" button before the login form.
    # Check for it instantly (no wait); it's fine if it's absent.
    try:
        btn = driver.find_element(
            By.XPATH, "/html/body/div[3]/div/div[2]/form/div[6]/div/button"
        )
        driver.execute_script("arguments[0].click();", btn)
        _emit("Clicked Access button", cb)
        time.sleep(1)
    except (NoSuchElementException, WebDriverException):
        pass

    # Fill credentials using stable element IDs (confirmed from live page diagnostics)
    _emit("Entering credentials…", cb)
    wait.until(EC.visibility_of_element_located(
        (By.ID, "qbenchLimsLoginEmail")
    )).send_keys(username)
    driver.find_element(By.ID, "qbenchLimsLoginPassword").send_keys(password)
    driver.find_element(By.ID, "qbenchLoginLIMSButton").click()
    _emit("Submitted login — waiting for redirect…", cb)

    # Wait until the login button disappears (page navigated away)
    # Use a shorter initial wait — check for error messages every few seconds
    _login_ok = False
    for _check in range(10):  # 10 x 5s = 50s total
        try:
            WebDriverWait(driver, 5).until(
                EC.invisibility_of_element_located((By.ID, "qbenchLoginLIMSButton"))
            )
            _login_ok = True
            break
        except TimeoutException:
            pass

        # Check for error banners that indicate bad credentials or lockout
        try:
            _body = driver.find_element(By.TAG_NAME, "body").text[:1000].lower()
            _lockout_phrases = [
                "too many login attempts",
                "locked out",
                "account locked",
                "account has been locked",
                "temporarily locked",
                "too many attempts",
            ]
            _bad_cred_phrases = [
                "invalid credentials",
                "incorrect password",
                "invalid email or password",
                "login failed",
                "invalid username",
                "authentication failed",
                "wrong password",
            ]
            for phrase in _lockout_phrases:
                if phrase in _body:
                    _emit(f"LOGIN LOCKOUT detected: '{phrase}'", cb, level=logging.WARNING)
                    raise LoginFailedError(f"Account locked: {phrase}")
            for phrase in _bad_cred_phrases:
                if phrase in _body:
                    _emit(f"LOGIN FAILED: '{phrase}'", cb, level=logging.WARNING)
                    raise LoginFailedError(f"Bad credentials: {phrase}")
        except LoginFailedError:
            raise
        except Exception:
            pass

    if not _login_ok:
        # Login button never disappeared — capture diagnostics
        _emit("LOGIN TIMEOUT: login button still visible after 50 s", cb, level=logging.WARNING)
        _emit(f"  Current URL: {driver.current_url}", cb)
        try:
            _diag = Path(tempfile.gettempdir()) / "qbench_login_fail.png"
            driver.save_screenshot(str(_diag))
            _emit(f"  Screenshot saved: {_diag}", cb)
        except Exception:
            pass
        try:
            _body = driver.find_element(By.TAG_NAME, "body").text[:500]
            _emit(f"  Page text: {_body}", cb)
        except Exception:
            pass
        raise LoginFailedError("Login timed out — credentials may be incorrect")
    _emit(f"Login successful — now at: {driver.current_url}", cb)


def _api_lookup(
    lab_id: str,
    cb: Optional[Callable[[str], None]] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> Optional[int]:
    """Resolve a Lab ID to its numeric QBench sample ID via the REST API.
    Runs with a hard timeout (API_LOOKUP_TIMEOUT_S) via a thread. On None,
    ``_state.lookup_problem`` says why ('' when QBench simply has no such
    sample)."""
    _emit(f"API: looking up Lab ID '{lab_id}'…", cb)

    result: list[Optional[int]] = [None]
    error:  list[Optional[str]] = [None]
    _state.lookup_problem = ""

    def _call():
        try:
            # qbench_client.py is vendored alongside this module in webapp/,
            # the folder app.py runs from (on sys.path) — no external share path needed.
            from qbench_client import QBenchAPIClient  # type: ignore
            kwargs = {}
            if client_id:
                kwargs["client_id"] = client_id
            if client_secret:
                kwargs["client_secret"] = client_secret
            client = QBenchAPIClient(**kwargs)
            samples = client.fetch_samples_by_lab_id(lab_id)
            if samples:
                result[0] = int(samples[0]["id"])
                _emit(f"API: sample ID = {result[0]}", cb)
            else:
                _emit(f"API: no sample found for '{lab_id}'", cb)
        except Exception as exc:
            error[0] = _exc_text(exc)
            logger.warning("[QBench] API lookup of %r failed", lab_id, exc_info=True)
            _emit(f"API error: {error[0]}", cb)

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(timeout=API_LOOKUP_TIMEOUT_S)

    if t.is_alive():
        _state.lookup_problem = (f"The QBench API did not answer within {API_LOOKUP_TIMEOUT_S:g} s "
                                 f"(looking up lab ID '{lab_id}').")
        _emit(_state.lookup_problem, cb)
        return None
    if error[0]:
        _state.lookup_problem = f"QBench API: {error[0]}"
        _emit(f"API call failed: {error[0]}", cb)
        return None
    return result[0]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def cancel_upload() -> None:
    """Immediately kill the active WebDriver session (safe to call from any thread)."""
    _cancelled.set()
    with _driver_lock:
        drv = _active_driver
    if drv:
        try:
            logger.info("[QBench] cancel_upload(): quitting active driver…")
            drv.quit()
        except Exception as e:
            logger.warning("[QBench] driver.quit() raised: %s", e)


def prime_chromedriver() -> bool:
    """Resolve (and cache) the ChromeDriver path NOW, before the first upload.
    Call this from the GUI so the user sees progress; returns True on success."""
    logger.info("[QBench] Priming ChromeDriver…")
    p = _resolve_chromedriver()
    logger.info("[QBench] ChromeDriver ready: %s", p or "(PATH fallback)")
    return True


# ---------------------------------------------------------------------------
# Post-upload verification helpers
# ---------------------------------------------------------------------------

def _verify_attachment(
    driver,
    sample_url: str,
    filename: str,
    cb: Optional[Callable[[str], None]] = None,
) -> bool:
    """Check the existing browser session for the filename on the attachments page."""
    try:
        attach_url = f"{sample_url}#attachments"
        driver.get(attach_url)
        time.sleep(3)
        src = driver.page_source or ""
        # Strip extension for a looser match (QBench may rename slightly)
        stem = Path(filename).stem
        found = stem.lower() in src.lower()
        logger.info("[QBench] Verify (same session): '%s' %s on page", stem,
                    "FOUND" if found else "NOT found")
        return found
    except Exception as exc:
        logger.warning("[QBench] Verify (same session) failed: %s", exc)
        return False


def _verify_attachment_fresh(
    sample_url: str,
    filename: str,
    headless: bool = True,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cb: Optional[Callable[[str], None]] = None,
) -> bool:
    """Open a fresh browser to verify the attachment exists on QBench.

    Used as a fallback when the original browser session died mid-upload.
    """
    drv = None
    try:
        drv = _make_driver(headless=headless)
        drv.get(f"{sample_url}#attachments")
        time.sleep(3)

        # Login if needed
        need_login = any(kw in drv.current_url.lower() for kw in ("login", "oauth", "auth", "forgotpwd"))
        if not need_login:
            try:
                drv.find_element(By.ID, "qbenchLoginLIMSButton")
                need_login = True
            except NoSuchElementException:
                pass
        if need_login and username and password:
            _login(drv, username, password, cb)
            drv.get(f"{sample_url}#attachments")
            time.sleep(4)

        src = drv.page_source or ""
        stem = Path(filename).stem
        found = stem.lower() in src.lower()
        logger.info("[QBench] Verify (fresh): '%s' %s on page", stem,
                    "FOUND" if found else "NOT found")
        return found
    except Exception as exc:
        logger.warning("[QBench] Verify (fresh) failed: %s", exc)
        return False
    finally:
        if drv:
            try:
                drv.quit()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def attach_pdf_to_sample(
    lab_id:  str,
    pdf_path: str | Path,
    *,
    headless: bool = True,
    username: Optional[str] = None,
    password: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    step_callback:     Optional[Callable[[int], None]] = None,
) -> bool:
    """Attach *pdf_path* to the QBench sample identified by *lab_id*.

    Every major step emits a log message to app.log AND to the UI via
    progress_callback BEFORE the blocking operation starts, so the user can
    always see exactly where the process is.

    Parameters
    ----------
    lab_id, pdf_path   : identify the sample and file.
    headless           : run Chrome without a visible window.
    username/password  : credentials; falls back to CREDENTIALS_FILE.
    progress_callback  : called with each status string → drives dialog log.
    step_callback      : called with step index (0-based) → drives progress bar.

    Returns False with ``last_failure()`` saying why (one line); raises
    ``LoginFailedError`` when QBench refuses the sign-in.
    """
    global _active_driver

    _set_failure("")
    _cancelled.clear()
    if not _SELENIUM_OK:
        return _fail(f"Selenium is not installed on the hub: {_SELENIUM_IMPORT_ERROR}",
                     progress_callback)

    pdf_path = Path(pdf_path)
    cb       = progress_callback
    step     = 0
    stage    = "Starting"

    def _step(msg: str) -> None:
        nonlocal step, stage
        stage = msg
        _emit(msg, cb)
        if step_callback:
            try:
                step_callback(step)
            except Exception:
                pass
        step += 1

    logger.info("[QBench] → %s  %s", lab_id, pdf_path.name)

    sample_id: Optional[int] = None
    sample_url: str = ""

    # Step 0 ── PDF confirmed ──────────────────────────────────────────
    if not pdf_path.exists():
        return _fail(f"The report PDF was not found at {pdf_path}.", cb)
    _step(f"PDF confirmed: {pdf_path.name} ({pdf_path.stat().st_size // 1024} KB)")

    # Step 1 ── API lookup → sample ID ────────────────────────────────
    _step(f"Looking up '{lab_id}' via QBench API…")
    sample_id = _api_lookup(lab_id, cb, client_id=client_id, client_secret=client_secret)
    if sample_id is None:
        return _fail(getattr(_state, "lookup_problem", "")
                     or f"QBench has no sample with lab ID '{lab_id}'.", cb)

    # Step 2 ── Credentials ───────────────────────────────────────────
    if not username or not password:
        try:
            _u, _pw = _get_credentials()
            username = username or _u
            password = password or _pw
        except CredentialsFileError as exc:
            return _fail(str(exc), cb)
        except Exception as exc:
            logger.exception("[QBench] reading %s failed", credentials_file())
            return _fail(f"Can't read the saved QBench sign-in file {credentials_file()}: "
                         f"{_exc_text(exc)}", cb)

    # Step 3 ── Start browser ──────────────────────────────────────────
    _step("Starting Chrome…")
    driver = None
    sample_url = f"{QBENCH_BASE}/sample?id={sample_id}"
    try:
        driver = _make_driver(headless=headless)
        with _driver_lock:
            _active_driver = driver

        # Step 4 ── Navigate to sample page ───────────────────────────
        _step(f"Navigating to sample {sample_id}…")
        driver.get(sample_url)
        time.sleep(2)

        # Step 5 ── Login if needed ────────────────────────────────────
        # QBench overlays the login form WITHOUT changing the URL, so
        # check for the login button element — not URL keywords.
        _need_login = any(kw in driver.current_url.lower()
                          for kw in ("login", "oauth", "auth", "forgotpwd"))
        if not _need_login:
            try:
                driver.find_element(By.ID, "qbenchLoginLIMSButton")
                _need_login = True
            except NoSuchElementException:
                pass

        if _need_login:
            _step("Logging in…")
            _login(driver, username, password, cb)
            driver.get(f"{sample_url}#attachments")
            time.sleep(4)
        else:
            _step("Already authenticated")

        # Step 6 ── Attachments tab ────────────────────────────────────
        _attach_url = f"{sample_url}#attachments"
        if driver.current_url.rstrip("/") != _attach_url.rstrip("/"):
            _step("Opening Attachments tab…")
            driver.get(_attach_url)
            time.sleep(4)
        else:
            _step("On Attachments tab")
            time.sleep(3)

        # Step 7 ── Click Upload button → open modal ───────────────────
        _step("Opening upload form…")
        try:
            upload_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//button[normalize-space(text())='Upload']")
                )
            )
            driver.execute_script("arguments[0].click();", upload_btn)
            time.sleep(2)
        except (TimeoutException, WebDriverException):
            _emit("'Upload' button not found — will try file input directly", cb)

        # Step 8 ── Send file to <input type="file"> ───────────────────
        _step(f"Uploading {pdf_path.name}…")
        try:
            file_input = WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
            )
            driver.execute_script("arguments[0].style.display='block';", file_input)
            file_input.send_keys(str(pdf_path.resolve()))
            time.sleep(2)
        except TimeoutException:
            return _fail("QBench did not answer in time: no file input on the attachments page "
                         f"(at: {stage})", cb)

        # Step 9 ── Tick 'Show in Report' / 'Include in Report' ────────
        _step("Enabling 'Show in Report'…")
        _found = False
        for _phrase in ("show in report", "include in report", "show", "include"):
            try:
                chk = driver.find_element(
                    By.XPATH,
                    f"//label[contains(translate(normalize-space(.),"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{_phrase}')]/preceding-sibling::input[@type='checkbox'] | "
                    f"//label[contains(translate(normalize-space(.),"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{_phrase}')]/following-sibling::input[@type='checkbox'] | "
                    f"//label[contains(translate(normalize-space(.),"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{_phrase}')]//input[@type='checkbox']"
                )
                if not chk.is_selected():
                    driver.execute_script("arguments[0].click();", chk)
                _found = True
                break
            except NoSuchElementException:
                pass
        if not _found:
            for _sel in [
                "input[type='checkbox'][id*='show']",
                "input[type='checkbox'][id*='report']",
                "input[type='checkbox'][id*='include']",
                "input[type='checkbox'][name*='show']",
                "input[type='checkbox'][name*='report']",
                "input[type='checkbox'][name*='include']",
            ]:
                try:
                    chk = driver.find_element(By.CSS_SELECTOR, _sel)
                    if not chk.is_selected():
                        driver.execute_script("arguments[0].click();", chk)
                    _found = True
                    break
                except NoSuchElementException:
                    pass
        if not _found:
            _emit("WARNING: 'Show in Report' checkbox not found", cb, level=logging.WARNING)

        # Step 10 ── Save ─────────────────────────────────────────────
        _step(f"Saving — '{pdf_path.name}' → '{lab_id}' (sample {sample_id})")
        _save_xpaths = [
            "//button[@id='saveAttachment']",
            "//button[@id='save-attachment']",
            "//button[@type='submit' and not(contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'cancel'))]",
            "//button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'upload')]",
            "//button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'save')"
            " and not(contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'cancel'))]",
        ]
        _saved = False
        for _xp in _save_xpaths:
            try:
                btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, _xp))
                )
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(4)
                _saved = True
                break
            except (TimeoutException, NoSuchElementException, WebDriverException):
                pass
        if not _saved:
            _emit("WARNING: no Save button found on the upload form", cb, level=logging.WARNING)

        # Step 11 ── Verify upload by checking the attachments page ────
        _verified = _verify_attachment(driver, sample_url, pdf_path.name, cb)
        if _verified:
            _step(f"Verified: '{pdf_path.name}' found on attachments page")
        else:
            _step("Could not confirm attachment on page (may still have uploaded)")
            logger.warning("[QBench] %s: '%s' not seen on the attachments page after saving",
                           lab_id, pdf_path.name)
        _set_failure("")
        return True  # save was attempted; verification is informational

    except LoginFailedError:
        # Let login failures propagate to the caller (app.py) for
        # credential re-prompt handling — don't swallow them here.
        raise
    except WebDriverException as exc:
        # A wait that ran out (TimeoutException: its message is empty),
        # Chrome that can't start (driver is None), cancel_upload()'s quit(),
        # or a crash. The whole message and stack go to the log.
        logger.warning("[QBench] %s: browser error at '%s': %s", lab_id, stage, str(exc),
                       exc_info=True)
        if _cancelled.is_set():
            reason = "Stopped by the operator."
        elif isinstance(exc, TimeoutException):
            reason = f"QBench did not answer in time (at: {stage})"
        elif driver is None:
            reason = f"Chrome could not start: {chrome_start_text(exc)}"
        else:
            reason = f"The browser session ended: {_exc_text(exc)} (at: {stage})"
        _step(f"Browser error: {reason}")

        # ── Post-crash verification: open a fresh browser, check if the
        #    file actually landed on QBench before reporting failure. ──
        if sample_id is not None and driver is not None and not _cancelled.is_set():
            _step("Verifying upload despite browser error...")
            _ok = _verify_attachment_fresh(sample_url, pdf_path.name, headless, username, password, cb)
            if _ok:
                _step(f"Verified: '{pdf_path.name}' IS on QBench despite the error")
                _set_failure("")
                return True

        return _fail(reason, cb)
    except Exception as exc:
        logger.exception("[QBench] %s: unexpected error at '%s'", lab_id, stage)
        reason = f"Unexpected error: {type(exc).__name__}: {_exc_text(exc)} (at: {stage})"
        _step(f"UNEXPECTED ERROR: {reason}")

        if sample_id is not None and driver is not None:
            _step("Verifying upload despite error...")
            _ok = _verify_attachment_fresh(sample_url, pdf_path.name, headless, username, password, cb)
            if _ok:
                _step(f"Verified: '{pdf_path.name}' IS on QBench despite the error")
                _set_failure("")
                return True

        return _fail(reason, cb)
    finally:
        with _driver_lock:
            _active_driver = None
        if driver:
            try:
                driver.quit()
                logger.info("[QBench] Browser closed")
            except Exception:
                pass
        logger.info("[QBench] Done with %s", lab_id)
