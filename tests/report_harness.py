"""Drive every report path in one app process and record what it reported.

Not a test module. ``tests/test_report_content.py`` runs it in a subprocess
(app.py must never be imported by the test process itself):

    python tests/report_harness.py <GC_DATA_DIR> <sample_id> <request.json> <out.json>

It imports app with ``GC_DATA_DIR`` pointing at a ``hub_boot`` data folder,
with two stand-ins installed first:

* a fake ``comments`` module (phase 4's seam: ``for_report`` returns fixed
  comments, one of them hostile markup; ``log_report`` records its rows);
* a fake QBench uploader (no Selenium, no network): ``attach_pdf_to_sample``
  keeps the PDF bytes and reports success.

Then it calls ``/api/analysis``, the direct export, the ZIP export and the
QBench upload with the same request, recording every ``_report_content``
output (tagged by path), every ``_report_html`` output, the PDFs' text and
the ``report_log`` rows, and writes them to ``out.json``.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import types
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONTENT_KEYS = ("items", "text", "conclusion_generated", "windows", "params_used",
                "comments", "ranges")
COMMENTS = [
    {"id": 7, "text": "Sample appears to be gasoline.", "initials": "RB",
     "created_at": "2026-09-29T10:00:00", "t0": None, "t1": None},
    {"id": 8, "text": "<img src=file:///etc/passwd> & more", "initials": "AB",
     "created_at": "2026-09-29T11:00:00", "t0": 1.3, "t1": 2.1},
    {"id": 9, "text": "Narrow mark", "initials": "CD",
     "created_at": "2026-09-29T12:00:00", "t0": 1.9, "t1": 2.1},
]


def _jsonable(x):
    try:
        import numpy as np
        if isinstance(x, np.generic):
            return x.item()
        if isinstance(x, np.ndarray):
            return x.tolist()
    except ImportError:
        pass
    raise TypeError(type(x))


def main(data_dir: str, sample_id: int, request_path: str, out_path: str) -> None:
    os.environ["GC_DATA_DIR"] = data_dir
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))

    log_rows: list[dict] = []
    fake = types.ModuleType("comments")
    fake.for_report = lambda sid, db: [dict(c) for c in COMMENTS]

    # Phase 4's exact signature (comments.log_report): keyword-only, so a
    # call that doesn't match it raises TypeError and the tests see no row.
    def log_report(sample_id, *, kind, revision=None, standard_name=None, params=None,
                   ranges=None, windows=None, bullets=None, bullets_text=None,
                   conclusion=None, conclusion_edited=None, comment_ids=None,
                   pdf_sha256=None, author_initials=None, author_ip=None,
                   app_version=None, created_at=None, user_name=None, db=None):
        log_rows.append(dict(
            sample_id=sample_id, kind=kind, revision=revision, standard_name=standard_name,
            params=params, ranges=ranges, windows=windows, bullets=bullets,
            bullets_text=bullets_text, conclusion=conclusion,
            conclusion_edited=conclusion_edited, comment_ids=comment_ids,
            pdf_sha256=pdf_sha256, author_initials=author_initials, author_ip=author_ip,
            user_name=user_name,
            app_version=app_version, created_at=created_at, db_given=db is not None))
        return len(log_rows)
    fake.log_report = log_report
    sys.modules["comments"] = fake

    import app  # noqa: E402  (after GC_DATA_DIR and the fake comments module)

    client = app.app.test_client()
    # Signed in (the session gate): a session row straight into the store,
    # as tests/bootapp.py does. No switch turns sign-in off.
    import hashlib
    import secrets
    from datetime import datetime, timedelta, timezone
    import store as _store
    token = secrets.token_urlsafe(32)
    _store.web_sessions.add(
        hashlib.sha256(token.encode()).hexdigest(), name="Report Harness", method="password",
        ip="127.0.0.1", user_agent="harness",
        expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        db=Path(data_dir) / _store.DB_FILENAME)
    client.set_cookie("gc_session", token)
    deadline = time.time() + 90
    while client.get("/api/files").status_code != 200:
        if time.time() > deadline:
            raise SystemExit("hub never started")
        time.sleep(0.25)

    current = {"path": None}
    records: list[dict] = []
    htmls: list[str] = []
    orig_content = app._report_content
    orig_html = app._report_html

    def rec_content(*a, **kw):
        out = orig_content(*a, **kw)
        records.append({"path": current["path"],
                        "content": json.loads(json.dumps({k: out[k] for k in CONTENT_KEYS},
                                                         default=_jsonable))})
        return out

    def rec_html(*a, **kw):
        out = orig_html(*a, **kw)
        htmls.append(out)
        return out
    app._report_content = rec_content
    app._report_html = rec_html

    pdfs: dict[str, bytes] = {}

    def fake_attach(lab_id, pdf_path, **kw):
        pdfs["qbench"] = Path(pdf_path).read_bytes()
        return True
    app.qbench_pdf_uploader.prime_chromedriver = lambda: True
    app.qbench_pdf_uploader.attach_pdf_to_sample = fake_attach
    app._load_qbench_credentials = lambda: ("", "")

    item = dict(request, sample_id=sample_id)
    out: dict = {"responses": {}}

    current["path"] = "analysis"
    r = client.post("/api/analysis", json=item)
    out["responses"]["analysis"] = {"status": r.status_code, "json": r.get_json()}

    current["path"] = "direct"
    r = client.post("/api/export-analysis-report", json=item)
    out["responses"]["direct"] = {"status": r.status_code}
    pdfs["direct"] = r.data

    current["path"] = "zip"
    # a background job since v3.0.1: 202 {job}, poll it, then the one-time download
    r = client.post("/api/export-analysis-reports-zip", json={"items": [item]})
    if r.status_code != 202:
        raise SystemExit(f"the ZIP export answered {r.status_code}: {r.get_data(as_text=True)}")
    job_id = r.get_json()["job"]["id"]
    deadline = time.time() + 180
    while True:
        job = client.get(f"/api/export-analysis-reports-zip/{job_id}").get_json()["job"]
        if job["state"] != "running":
            break
        if time.time() > deadline:
            raise SystemExit("the ZIP export never finished")
        time.sleep(0.1)
    r = client.get(job["download"]) if job["download"] else r
    out["responses"]["zip"] = {"status": r.status_code, "job_state": job["state"]}
    with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
        pdfs["zip"] = zf.read(zf.namelist()[0])

    current["path"] = "qbench"
    live_cursor = app.live.BUS.cursor()
    r = client.post("/api/qbench-upload", json={"queue": [item]})
    out["responses"]["qbench"] = {"status": r.status_code, "json": r.get_json()}
    deadline = time.time() + 180
    time.sleep(0.5)
    while app._upload_thread is not None and app._upload_thread.is_alive():
        if time.time() > deadline:
            raise SystemExit("QBench upload thread never finished")
        time.sleep(0.25)
    # v3.1 live updates: the upload record publishes the sample
    out["live_after_qbench"] = app.live.BUS.since(live_cursor)
    # the status the report queue matches its reports against (by sample_id)
    out["upload_status"] = client.get("/api/qbench-upload-status").get_json()

    import pypdf
    out["pdf_text"] = {
        k: " ".join(" ".join((p.extract_text() or "") for p in
                             pypdf.PdfReader(io.BytesIO(v)).pages).split())
        for k, v in pdfs.items()}
    out["records"] = records
    out["htmls"] = htmls
    out["log_rows"] = log_rows
    Path(out_path).write_text(json.dumps(out, default=_jsonable), encoding="utf-8")
    os._exit(0)          # skip the hub threads' shutdown


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4])
