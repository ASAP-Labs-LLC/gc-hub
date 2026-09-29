"""download_jobs.py (v3.0.1): a file built in the background, then fetched
once through a one-time link. The report ZIP uses it, because building N
report PDFs in the request outlasted Cloudflare's 100 s (HTTP 524 through
https://gc.asaplabs.net). Stdlib only, tested in-process."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import download_jobs  # noqa: E402


def _wait(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


@pytest.fixture
def jobs(tmp_path):
    return download_jobs.DownloadJobs(lambda: tmp_path / "zips", max_running=2, ttl=600)


def _writer(release=None, text=b"zip bytes", summary=None):
    def fn(progress, out_path):
        progress(done=1, total=2)
        if release is not None:
            assert release.wait(10)
        progress(done=2, total=2)
        out_path.write_bytes(text)
        return summary or {"written": 2, "skipped": 0}
    return fn


def test_start_answers_at_once_then_the_file_is_fetched_once(jobs, tmp_path):
    release = threading.Event()
    t0 = time.monotonic()
    job = jobs.start("reports-zip", _writer(release), name="analysis_reports.zip",
                     owner="Ryan C", total=2)
    assert time.monotonic() - t0 < 1
    assert job["state"] == "running" and job["kind"] == "reports-zip"
    assert len(job["id"]) >= 32                     # random: the id is the link
    assert jobs.busy() is True
    assert _wait(lambda: jobs.status(job["id"], "Ryan C")["done"] == 1)
    assert jobs.status(job["id"], "Ryan C")["total"] == 2
    assert jobs.claim(job["id"], "Ryan C") is None  # nothing to fetch yet
    release.set()
    assert _wait(lambda: jobs.status(job["id"], "Ryan C")["state"] != "running")
    done = jobs.status(job["id"], "Ryan C")
    assert done["state"] == "done", done
    assert done["result"] == {"written": 2, "skipped": 0}
    assert done["size"] == len(b"zip bytes") and done["name"] == "analysis_reports.zip"
    assert "path" not in done                       # never the server path
    assert jobs.busy() is True                      # waiting to be fetched

    got = jobs.claim(job["id"], "Ryan C")
    assert got["name"] == "analysis_reports.zip"
    assert Path(got["path"]).read_bytes() == b"zip bytes"
    assert jobs.claim(job["id"], "Ryan C") is None  # single use
    assert jobs.status(job["id"], "Ryan C")["state"] == "streaming"
    assert jobs.busy() is True
    jobs.finished_streaming(got)
    assert jobs.status(job["id"], "Ryan C")["state"] == "fetched"
    assert not Path(got["path"]).exists()
    assert jobs.busy() is False


def test_only_the_starter_sees_or_fetches_it(jobs):
    job = jobs.start("reports-zip", _writer(), name="a.zip", owner="Ryan C", total=2)
    assert _wait(lambda: jobs.status(job["id"], "Ryan C")["state"] == "done")
    assert jobs.status(job["id"], "Someone Else") is None
    assert jobs.claim(job["id"], "Someone Else") is None
    assert jobs.status("nope", "Ryan C") is None
    assert jobs.claim(job["id"], "Ryan C") is not None


def test_a_failure_is_reported_and_leaves_no_file(jobs, tmp_path):
    def fn(progress, out_path):
        out_path.write_bytes(b"half")
        raise download_jobs.NothingToDownload("All 3 report(s) were skipped")
    job = jobs.start("reports-zip", fn, name="a.zip", owner="R", total=3)
    assert _wait(lambda: jobs.status(job["id"], "R")["state"] != "running")
    st = jobs.status(job["id"], "R")
    assert st["state"] == "failed" and st["error"] == "All 3 report(s) were skipped"
    assert jobs.claim(job["id"], "R") is None
    assert list((tmp_path / "zips").iterdir()) == []
    assert jobs.busy() is False

    def boom(progress, out_path):
        raise OSError("disk gone")
    job = jobs.start("reports-zip", boom, name="a.zip", owner="R", total=1)
    assert _wait(lambda: jobs.status(job["id"], "R")["state"] != "running")
    assert jobs.status(job["id"], "R")["error"] == "OSError: disk gone"


def test_at_most_max_running_builds(jobs):
    release = threading.Event()
    try:
        jobs.start("reports-zip", _writer(release), name="a.zip", owner="A", total=2)
        jobs.start("reports-zip", _writer(release), name="b.zip", owner="B", total=2)
        with pytest.raises(download_jobs.Busy):
            jobs.start("reports-zip", _writer(release), name="c.zip", owner="C", total=2)
    finally:
        release.set()
    assert _wait(lambda: not jobs.running())
    jobs.start("reports-zip", _writer(), name="d.zip", owner="D", total=2)


def test_an_unfetched_file_expires(tmp_path, monkeypatch):
    jobs = download_jobs.DownloadJobs(lambda: tmp_path / "zips", ttl=600)
    job = jobs.start("reports-zip", _writer(), name="a.zip", owner="R", total=2)
    assert _wait(lambda: jobs.status(job["id"], "R")["state"] == "done")
    files = list((tmp_path / "zips").iterdir())
    assert len(files) == 1
    real = time.time
    monkeypatch.setattr(download_jobs.time, "time", lambda: real() + 601)
    assert jobs.busy() is False
    assert jobs.status(job["id"], "R") is None
    assert jobs.claim(job["id"], "R") is None
    assert not files[0].exists()


def test_cleanup_removes_leftovers_of_a_killed_process(tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    (folder / "old.zip.part").write_bytes(b"x")
    jobs = download_jobs.DownloadJobs(lambda: folder)
    jobs.cleanup()
    assert list(folder.iterdir()) == []
