"""Boot app.py with the real ``qbench_pdf_uploader``, minus Chrome (tests only).

``bootapp.booted(tmp, cmd=[python, tests/qbench_stub_boot.py, "--no-tray"])``
runs this instead of ``app.py``: it imports the real uploader, stops it from
resolving or downloading a ChromeDriver, wraps ``attach_pdf_to_sample`` with
a plan read on every call from the JSON file named by
``GC_TEST_QBENCH_PLAN`` (``{lab_id: action}``), then runs app.py as
``__main__``, exactly as the updater does (cwd = the repo root).

Actions:

* ``real`` (the default): the real ``attach_pdf_to_sample`` (point
  ``QBENCH_TOKEN_URL`` at a closed port and it fails at the API lookup);
* ``ok``: uploaded;
* ``false:<reason>``: returns False with ``last_failure()`` = reason;
* ``boom``: raises RuntimeError with a two-line message;
* ``slow``: waits 3 s (or until the upload is stopped), then uploaded;
* ``login``: raises ``LoginFailedError``.

Not a test module (no ``test_`` prefix).
"""
from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import qbench_pdf_uploader as q  # noqa: E402

q._resolve_chromedriver = lambda: None          # never download a driver in a test
_real_attach = q.attach_pdf_to_sample


def _plan(lab_id: str) -> str:
    path = os.environ.get("GC_TEST_QBENCH_PLAN")
    if not path or not Path(path).exists():
        return "real"
    return str(json.loads(Path(path).read_text(encoding="utf-8")).get(lab_id, "real"))


def attach_pdf_to_sample(lab_id, pdf_path, **kw):
    action = _plan(lab_id)
    q._set_failure("")
    if action == "ok":
        return True
    if action.startswith("false:"):
        q._set_failure(action[len("false:"):])
        return False
    if action == "boom":
        raise RuntimeError("kaboom in the uploader\nsecond line of detail")
    if action == "login":
        raise q.LoginFailedError("Bad credentials: invalid credentials")
    if action == "slow":
        q._cancelled.clear()
        q._cancelled.wait(3)
        if q._cancelled.is_set():
            q._set_failure("Stopped by the operator.")
            return False
        return True
    return _real_attach(lab_id, pdf_path, **kw)


q.attach_pdf_to_sample = attach_pdf_to_sample

os.chdir(ROOT)
sys.argv = [str(ROOT / "app.py"), *sys.argv[1:]]
runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
