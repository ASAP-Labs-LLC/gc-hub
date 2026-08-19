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
"""
from __future__ import annotations

import logging
import os
import sys
import time
import threading
import traceback
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _emit(msg: str, cb: Optional[Callable[[str], None]] = None) -> None:
    """Print to terminal AND forward to the UI callback."""
    print(f"[QBench] {msg}", flush=True)
    logger.info("[QBench] %s", msg)
    if cb:
        try:
            cb(msg)
        except Exception as _exc:
            print(f"[QBench] callback error: {_exc}", flush=True)


def _get_credentials() -> tuple[str, str]:
    _emit(f"Reading credentials from {CREDENTIALS_FILE}")
    with open(CREDENTIALS_FILE, "r") as fh:
        lines = fh.readlines()
    if len(lines) < 2:
        raise ValueError("qbenchlogin.txt must have username on line 1, password on line 2")
    _emit("Credentials loaded OK")
    return lines[0].strip(), lines[1].strip()


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
            print(f"[QBench] ChromeDriver cached at {exe}", flush=True)
            return exe
    except Exception as e:
        print(f"[QBench] webdriver-manager failed: {e}", flush=True)

    # Try chromedriver-autoinstaller
    try:
        import chromedriver_autoinstaller
        p = chromedriver_autoinstaller.install()
        if p and os.path.exists(str(p)):
            _driver_path = str(p)
            print(f"[QBench] chromedriver-autoinstaller path: {_driver_path}", flush=True)
            return _driver_path
    except Exception as e:
        print(f"[QBench] chromedriver-autoinstaller failed: {e}", flush=True)

    # Fall back to PATH
    print("[QBench] Will rely on chromedriver in PATH", flush=True)
    return None


def _make_driver(headless: bool = True) -> object:
    options = _ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1600,900")
    options.add_argument("--disable-extensions")

    cd_path = _driver_path  # Use cached path (resolved before worker starts)
    if cd_path and os.path.exists(cd_path):
        driver = webdriver.Chrome(service=_ChromeService(cd_path), options=options)
    else:
        driver = webdriver.Chrome(options=options)
    # Log browser version for debugging version-mismatch issues
    try:
        caps = driver.capabilities
        print(f"[QBench] Chrome {caps.get('browserVersion','?')}  "
              f"ChromeDriver {caps.get('chrome',{}).get('chromedriverVersion','?').split()[0]}",
              flush=True)
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
                    _emit(f"LOGIN LOCKOUT detected: '{phrase}'", cb)
                    raise LoginFailedError(f"Account locked: {phrase}")
            for phrase in _bad_cred_phrases:
                if phrase in _body:
                    _emit(f"LOGIN FAILED: '{phrase}'", cb)
                    raise LoginFailedError(f"Bad credentials: {phrase}")
        except LoginFailedError:
            raise
        except Exception:
            pass

    if not _login_ok:
        # Login button never disappeared — capture diagnostics
        _emit("LOGIN TIMEOUT: login button still visible after 50 s", cb)
        _emit(f"  Current URL: {driver.current_url}", cb)
        try:
            _diag = Path(os.environ.get("TEMP", ".")) / "qbench_login_fail.png"
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
    Runs with a 20-second hard timeout via a thread."""
    _emit(f"API: looking up Lab ID '{lab_id}'…", cb)

    result: list[Optional[int]] = [None]
    error:  list[Optional[str]] = [None]

    def _call():
        try:
            # qbench_client.py is vendored alongside this module in webapp/,
            # which run.pyw puts on sys.path — no external share path needed.
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
            error[0] = str(exc)
            _emit(f"API error: {exc}", cb)

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(timeout=25)  # 25-second hard timeout

    if t.is_alive():
        _emit("API call timed out after 25 s — QBench API may be unreachable", cb)
        return None
    if error[0]:
        _emit(f"API call failed: {error[0]}", cb)
        return None
    return result[0]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def cancel_upload() -> None:
    """Immediately kill the active WebDriver session (safe to call from any thread)."""
    with _driver_lock:
        drv = _active_driver
    if drv:
        try:
            print("[QBench] cancel_upload(): quitting active driver…", flush=True)
            drv.quit()
        except Exception as e:
            print(f"[QBench] driver.quit() raised: {e}", flush=True)


def prime_chromedriver() -> bool:
    """Resolve (and cache) the ChromeDriver path NOW, before the first upload.
    Call this from the GUI so the user sees progress; returns True on success."""
    print("[QBench] Priming ChromeDriver…", flush=True)
    p = _resolve_chromedriver()
    print(f"[QBench] ChromeDriver ready: {p or '(PATH fallback)'}", flush=True)
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
        print(f"[QBench] Verify (same session): '{stem}' {'FOUND' if found else 'NOT found'} on page", flush=True)
        return found
    except Exception as exc:
        print(f"[QBench] Verify (same session) failed: {exc}", flush=True)
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
        print(f"[QBench] Verify (fresh): '{stem}' {'FOUND' if found else 'NOT found'} on page", flush=True)
        return found
    except Exception as exc:
        print(f"[QBench] Verify (fresh) failed: {exc}", flush=True)
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

    Every major step emits a log message to the terminal AND to the UI via
    progress_callback BEFORE the blocking operation starts, so the user can
    always see exactly where the process is.

    Parameters
    ----------
    lab_id, pdf_path   : identify the sample and file.
    headless           : run Chrome without a visible window.
    username/password  : credentials; falls back to CREDENTIALS_FILE.
    progress_callback  : called with each status string → drives dialog log.
    step_callback      : called with step index (0-based) → drives progress bar.
    """
    global _active_driver

    if not _SELENIUM_OK:
        _emit(f"Selenium not installed: {_SELENIUM_IMPORT_ERROR}", progress_callback)
        return False

    pdf_path = Path(pdf_path)
    cb       = progress_callback
    step     = 0

    def _step(msg: str) -> None:
        nonlocal step
        _emit(msg, cb)
        if step_callback:
            try:
                step_callback(step)
            except Exception:
                pass
        step += 1

    print(f"[QBench] → {lab_id}  {pdf_path.name}", flush=True)

    sample_id: Optional[int] = None
    sample_url: str = ""

    # Step 0 ── PDF confirmed ──────────────────────────────────────────
    if not pdf_path.exists():
        _emit(f"ERROR: PDF not found at {pdf_path}", cb)
        return False
    _step(f"PDF confirmed: {pdf_path.name} ({pdf_path.stat().st_size // 1024} KB)")

    # Step 1 ── API lookup → sample ID ────────────────────────────────
    _step(f"Looking up '{lab_id}' via QBench API…")
    sample_id = _api_lookup(lab_id, cb, client_id=client_id, client_secret=client_secret)
    if sample_id is None:
        _emit(f"FAILED: No QBench sample found for '{lab_id}'", cb)
        return False

    # Step 2 ── Credentials ───────────────────────────────────────────
    if not username or not password:
        try:
            _u, _pw = _get_credentials()
            username = username or _u
            password = password or _pw
        except Exception as exc:
            _emit(f"FAILED: Cannot read credentials — {exc}", cb)
            return False

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
            _emit("ERROR: No <input type='file'> found on page", cb)
            return False

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
            _emit("WARNING: 'Show in Report' checkbox not found", cb)

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
        for _xp in _save_xpaths:
            try:
                btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, _xp))
                )
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(4)
                break
            except (TimeoutException, NoSuchElementException, WebDriverException):
                pass

        # Step 11 ── Verify upload by checking the attachments page ────
        _verified = _verify_attachment(driver, sample_url, pdf_path.name, cb)
        if _verified:
            _step(f"Verified: '{pdf_path.name}' found on attachments page")
        else:
            _step("Could not confirm attachment on page (may still have uploaded)")
        return True  # save was attempted; verification is informational

    except LoginFailedError:
        # Let login failures propagate to the caller (app.py) for
        # credential re-prompt handling — don't swallow them here.
        raise
    except WebDriverException as exc:
        # Raised when driver.quit() is called by cancel_upload() or Chrome crash
        _msg = str(exc).strip() or "(empty — Chrome process likely crashed)"
        _step(f"Browser session ended: {_msg}")
        print(f"[QBench] WebDriverException: {_msg}", flush=True)

        # ── Post-crash verification: open a fresh browser, check if the
        #    file actually landed on QBench before reporting failure. ──
        if sample_id is not None:
            _step("Verifying upload despite browser error...")
            _ok = _verify_attachment_fresh(sample_url, pdf_path.name, headless, username, password, cb)
            if _ok:
                _step(f"Verified: '{pdf_path.name}' IS on QBench despite the error")
                return True

        return False
    except Exception as exc:
        _step(f"UNEXPECTED ERROR: {exc}")
        traceback.print_exc()

        if sample_id is not None:
            _step("Verifying upload despite error...")
            _ok = _verify_attachment_fresh(sample_url, pdf_path.name, headless, username, password, cb)
            if _ok:
                _step(f"Verified: '{pdf_path.name}' IS on QBench despite the error")
                return True

        return False
    finally:
        with _driver_lock:
            _active_driver = None
        if driver:
            try:
                driver.quit()
                print("[QBench] Browser closed", flush=True)
            except Exception:
                pass
        print(f"[QBench] Done with {lab_id}\n", flush=True)
