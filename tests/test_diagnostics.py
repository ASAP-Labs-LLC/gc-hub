"""The admin "Download diagnostics" bundle (diagnostics.build_bundle).

A data folder is seeded with every secret the hub keeps (the admin password
hash, agent token hashes, the setup code, the QBench credential store and the
Selenium login file, secret-looking settings.json keys), each carrying the
same distinctive MARKER, plus a log that mentions the setup code the way
admin_auth really logs it. The bundle must never contain MARKER anywhere: not
in a zip member, not in the database copy's bytes.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import diagnostics  # noqa: E402
import store  # noqa: E402

MARKER = "ZQXSEKRIT7f3a"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class Seeded:
    """A data folder with samples, CDFs, exports, logs, reports and secrets."""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.data = root / "data"
        self.data.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.db = self.data / store.DB_FILENAME
        store.migrate(self.db)
        now = datetime.now(timezone.utc)
        self.now = now

        # ── secrets ──
        store.instruments.upsert({"id": "gc1", "name": "GC-1",
                                  "token_hash": f"tokhash-{MARKER}-1",
                                  "token_issued_at": _iso(now)}, db=self.db)
        store.instruments.upsert({"id": "gc2", "name": "GC-2",
                                  "token_hash": f"tokhash-{MARKER}-2"}, db=self.db)
        store.settings_kv.set("admin_password",
                              f"pbkdf2_sha256$310000${MARKER}salt${MARKER}hash", db=self.db)
        store.settings_kv.set("some_api_token", f"tok-{MARKER}", db=self.db)
        store.settings_kv.set("hub_url", "http://asapsv1:5560", db=self.db)
        (self.data / "admin-setup-code.txt").write_text(f"code-{MARKER}\n", encoding="utf-8")
        (self.data / "qbenchlogin.txt").write_text(f"user-{MARKER}\npw-{MARKER}\n",
                                                   encoding="utf-8")
        qstore = {"client_id": f"cid-{MARKER}", "client_secret": f"csec-{MARKER}"}
        # a store inside the data folder (QBENCH_STORE_PATH aimed there) ...
        (self.data / "qbench.json").write_text(json.dumps(qstore), encoding="utf-8")
        monkeypatch.setenv("QBENCH_STORE_PATH", str(self.data / "qbench.json"))
        # ... and the default one outside it, plus a corrupt-copy leftover
        (self.home / "qbench.json").write_text(json.dumps(qstore), encoding="utf-8")
        (self.data / "qbench.json.corrupt-20260101-000000").write_text(
            json.dumps(qstore), encoding="utf-8")
        self.settings = {
            "processed_cdf_dir": str(self.data / "processed_cdf"),
            "correction_factors_json": "//asapserver/share/correction_factors.json",
            "export_folder": str(self.data / "exports"),
            "qbench_password": f"pw-{MARKER}",
            "nested": {"client_secret": f"ns-{MARKER}", "folder": "C:/x"},
            "analysis_window": "301",
        }
        (self.data / "settings.json").write_text(json.dumps(self.settings), encoding="utf-8")

        # ── logs (the setup code is logged at start-up, as admin_auth does) ──
        (self.data / "app.log").write_text(
            "2026-09-29 [INFO] webapp: started\n"
            f"2026-09-29 [WARNING] admin_auth: admin setup code: code-{MARKER} (written to x)\n"
            "2026-09-29 [ERROR] hub: something broke\n", encoding="utf-8")
        (self.data / "app.log.1").write_text("older line\n" * 100, encoding="utf-8")

        # ── notifications ──
        (self.data / "notifications.json").write_text(json.dumps(
            [{"id": "n1", "ts": "2026-09-29T00:00:00", "level": "error", "message": "boom"}]),
            encoding="utf-8")

        # ── samples and CDFs ──
        def cdf(rel: str, payload: bytes = b"CDF\x01fake-netcdf") -> str:
            p = self.data / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(payload)
            return rel

        recent = _iso(now - timedelta(days=2))
        old = _iso(now - timedelta(days=60))
        self.ids = {}
        for n, (status, when, note) in enumerate([
                ("final", recent, None), ("error", recent, None),
                ("awaiting_calibration", recent, None), ("pending_corrections", recent, None),
                ("review_method", recent, None), ("other_method", recent, None),
                ("final", recent, "check this one"), ("error", old, None)]):
            rel = cdf(f"cdf/gc1/2026/09/S{n}.CDF")
            sid = store.samples.insert_received(
                "gc1", f"L{n}", f"2026-09-2{n % 9}T10:00:0{n}", "cdf", cdf_sha256=f"sha{n}",
                cdf_path=rel, status=status, received_at=when, db=self.db)
            if note:
                store.samples.update(sid, review_note=note, db=self.db)
            self.ids[(status, when, note)] = (sid, rel)
        self.final_rel = f"cdf/gc1/2026/09/S0.CDF"
        self.noted_rel = f"cdf/gc1/2026/09/S6.CDF"
        self.old_error_rel = f"cdf/gc1/2026/09/S7.CDF"
        self.problem_rels = [f"cdf/gc1/2026/09/S{n}.CDF" for n in (1, 2, 3, 4, 5, 6)]
        self.conflict_rel = cdf("cdf/gc1/conflicts/2026/09/C1.CDF")
        store.conflicts.add("gc1", "L1", "2026-09-21T10:00:01", None, "shaC", self.conflict_rel,
                            received_at=recent, db=self.db)

        # ── exports: gc1 at the default path, with a sidecar and 30 rows ──
        self.export = self.data / "results" / "gc1_results.csv"
        self.export.parent.mkdir(parents=True)
        self.export.write_text("".join(f"row{i}\n" for i in range(30)), encoding="utf-8")
        (self.data / "results" / "gc1_results.csv.gchub.json").write_text(
            json.dumps({"instrument": "gc1", "size": 1, "seq": 0, "sha256": "x"}),
            encoding="utf-8")

        # ── reports: 25, of which the newest 20 go in ──
        rep = self.data / "reports"
        rep.mkdir()
        import os
        for i in range(25):
            p = rep / f"parity-{i:02d}.md"
            p.write_text(f"report {i}\n", encoding="utf-8")
            os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))

        # ── updater files ──
        self.staged = self.data / "staged.json"
        self.staged.write_text(json.dumps({"tag": "v3.0.1", "healthy": True}), encoding="utf-8")
        (self.data / "switch-refused").write_text("reason: busy", encoding="utf-8")

    def build(self, options=None, **kw):
        out = self.root / "out.zip"
        manifest = diagnostics.build_bundle(options or {}, data_dir=self.data, db=self.db,
                                            out_path=out, who="admin@127.0.0.1", **kw)
        return out, manifest


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "home" / "AppData"))
    monkeypatch.setenv("QBENCH_CLIENT_SECRET", f"envsec-{MARKER}")
    return Seeded(tmp_path, monkeypatch)


def _members(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def _assert_no_marker(members: dict):
    for name, data in members.items():
        assert MARKER.encode() not in data, f"secret marker found in {name}"
        assert MARKER.encode("utf-16-le") not in data, f"secret marker (utf-16) found in {name}"
        assert MARKER not in name, name


ALL_ON = {k: True for k in diagnostics.OPTION_KEYS}


def test_marker_appears_nowhere_with_every_option_on(seeded):
    out, manifest = seeded.build(ALL_ON)
    members = _members(out)
    _assert_no_marker(members)
    names = set(members)
    for banned in ("admin-setup-code.txt", "qbenchlogin.txt", "qbench.json"):
        assert not any(n.rsplit("/", 1)[-1].startswith(banned) for n in names), banned
    assert "database/gc.db" in names


def test_manifest_and_summary(seeded):
    out, manifest = seeded.build()
    members = _members(out)
    doc = json.loads(members["manifest.json"])
    assert doc == json.loads(json.dumps(manifest))
    for key in ("app_version", "python", "platform", "disk", "uptime_seconds", "runtime",
                "updater", "created_at", "who", "options", "files", "status_snapshot"):
        assert key in doc, key
    assert doc["who"] == "admin@127.0.0.1"
    assert doc["options"] == diagnostics.normalize_options({})
    import hashlib
    listed = {f["name"]: f for f in doc["files"]}
    assert "manifest.json" not in listed
    assert set(listed) == set(members) - {"manifest.json"}
    for name, f in listed.items():
        assert f["size"] == len(members[name])
        assert f["sha256"] == hashlib.sha256(members[name]).hexdigest()
    assert b"GC hub diagnostics" in members["summary.txt"]
    # updater files are read (never modified)
    assert doc["updater"]["staged.json"]["content"] == {"tag": "v3.0.1", "healthy": True}
    assert "switch-refused" in doc["updater"]
    assert seeded.staged.read_text(encoding="utf-8") == json.dumps({"tag": "v3.0.1",
                                                                     "healthy": True})
    assert (seeded.data / "switch-refused").exists()
    assert doc["runtime"] == {"running": False}


def test_default_options():
    opts = diagnostics.normalize_options({})
    assert opts == {"summary": True, "logs": True, "tables": True, "settings": True,
                    "database": True, "exports": True, "reports": True, "problem_cdfs": True,
                    "all_cdfs": False}
    assert diagnostics.normalize_options({"all_cdfs": True})["all_cdfs"] is True
    with pytest.raises(ValueError):
        diagnostics.normalize_options({"nope": True})
    with pytest.raises(ValueError):
        diagnostics.normalize_options({"logs": "yes"})
    with pytest.raises(ValueError):
        diagnostics.normalize_options(["logs"])


def test_logs_are_redacted_and_capped(seeded, monkeypatch):
    out, _ = seeded.build()
    members = _members(out)
    log = members["logs/app.log"].decode()
    assert "something broke" in log and "[REDACTED]" in log
    assert "logs/app.log.1" in members
    monkeypatch.setattr(diagnostics, "LOG_CAP_BYTES", 200)
    out, manifest = seeded.build()
    members = _members(out)
    total = sum(len(v) for k, v in members.items() if k.startswith("logs/"))
    assert total <= 200
    assert any(s["reason"].startswith("log cap") for s in manifest["skipped"])


def test_tables(seeded):
    out, _ = seeded.build()
    m = _members(out)
    inst = json.loads(m["tables/instruments.json"])
    assert {r["id"] for r in inst} == {"gc1", "gc2"}
    assert all("token_hash" not in r for r in inst)
    assert {r["id"]: r["has_token"] for r in inst} == {"gc1": True, "gc2": True}
    counts = json.loads(m["tables/sample_counts.json"])
    assert {"instrument_id": "gc1", "status": "error", "backfill": 0, "n": 2} in counts
    for t in ("notifications", "jobs", "agents", "import_runs", "report_log", "conflicts",
              "settings_kv"):
        assert f"tables/{t}.json" in m, t
    assert json.loads(m["tables/notifications.json"])[0]["message"] == "boom"
    kv = {r["key"]: r["value"] for r in json.loads(m["tables/settings_kv.json"])}
    assert "admin_password" not in kv and "some_api_token" not in kv
    assert kv["hub_url"] == "http://asapsv1:5560"


def test_jobs_keep_open_and_last_done(seeded, monkeypatch):
    monkeypatch.setattr(diagnostics, "JOBS_DONE_LAST", 3)
    with store.connection(seeded.db) as conn:
        for i in range(10):
            conn.execute("INSERT INTO jobs(kind, payload, state, created_at) VALUES (?,?,?,?)",
                         ("process", "{}", "done" if i < 8 else "queued", "2026"))
    out, _ = seeded.build()
    jobs = json.loads(_members(out)["tables/jobs.json"])
    assert sum(j["state"] == "queued" for j in jobs) == 2
    assert sum(j["state"] == "done" for j in jobs) == 3


def test_settings_redacted_paths_kept(seeded):
    out, _ = seeded.build()
    s = json.loads(_members(out)["settings/settings.json"])
    assert s["qbench_password"] == "[REDACTED]"
    assert s["nested"]["client_secret"] == "[REDACTED]"
    assert s["nested"]["folder"] == "C:/x"
    assert s["processed_cdf_dir"] == seeded.settings["processed_cdf_dir"]
    assert s["correction_factors_json"] == seeded.settings["correction_factors_json"]
    assert s["analysis_window"] == "301"


def test_database_copy_is_consistent_and_scrubbed(seeded, tmp_path):
    out, _ = seeded.build()
    raw = _members(out)["database/gc.db"]
    p = tmp_path / "copy.db"
    p.write_bytes(raw)
    conn = sqlite3.connect(p)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        keys = {r[0] for r in conn.execute("SELECT key FROM settings_kv")}
        assert "admin_password" not in keys and "some_api_token" not in keys
        assert "hub_url" in keys
        assert conn.execute("SELECT count(*) FROM instruments WHERE token_hash IS NOT NULL"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM samples").fetchone()[0] == 8
    finally:
        conn.close()
    # the live database is untouched
    assert store.settings_kv.get("admin_password", db=seeded.db).startswith("pbkdf2")


def test_build_does_not_need_the_write_lock(seeded):
    """A writer holding BEGIN IMMEDIATE (and keeping it) does not block a build."""
    writer = store.open_db(seeded.db)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO settings_kv(key, value) VALUES ('mid', 'x')")
        done = {}
        t = threading.Thread(target=lambda: done.setdefault("m", seeded.build()))
        t.start()
        t.join(30)
        assert not t.is_alive() and "m" in done
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_exports_health(seeded):
    with store.connection(seeded.db) as conn, store.write_txn(conn):
        conn.execute("INSERT INTO sample_results(sample_id, revision, results, reason, "
                      "processed_at) VALUES (1, 1, '{}', 'processed', 'x')")
        conn.execute("INSERT INTO export_rows(instrument_id, sample_id, revision, \"row\") "
                     "VALUES ('gc1', 1, 1, 'a,b')")
    out, _ = seeded.build()
    m = _members(out)
    health = {r["instrument"]: r for r in json.loads(m["exports/health.json"])}
    gc1 = health["gc1"]
    assert Path(gc1["path"]) == seeded.export
    assert gc1["exists"] and gc1["size"] == seeded.export.stat().st_size
    assert gc1["sidecar"]["instrument"] == "gc1"
    assert gc1["lock_present"] is False
    assert gc1["pending"] == 1
    assert gc1["tail"] == [f"row{i}" for i in range(10, 30)]
    assert health["gc2"]["exists"] is False


def test_reports_newest_twenty(seeded):
    out, _ = seeded.build()
    names = sorted(n for n in _members(out) if n.startswith("reports/"))
    assert names == [f"reports/parity-{i:02d}.md" for i in range(5, 25)]


def test_problem_cdfs(seeded, monkeypatch):
    out, manifest = seeded.build()
    names = set(_members(out))
    for rel in seeded.problem_rels + [seeded.conflict_rel]:
        assert rel in names, rel
    assert seeded.final_rel not in names          # final, no note
    assert seeded.old_error_rel not in names      # older than 30 days
    # the cap: what doesn't fit is listed as skipped
    monkeypatch.setattr(diagnostics, "PROBLEM_CDF_CAP_BYTES", 40)
    out, manifest = seeded.build()
    names = set(_members(out))
    included = [r for r in seeded.problem_rels + [seeded.conflict_rel] if r in names]
    assert 0 < len(included) < 7
    skipped = [s for s in manifest["skipped"] if s["reason"].startswith("problem CDF cap")]
    assert len(skipped) == 7 - len(included)


def test_all_cdfs_off_by_default_on_when_asked(seeded):
    out, _ = seeded.build()
    assert seeded.final_rel not in _members(out)
    out, _ = seeded.build({"all_cdfs": True})
    names = set(_members(out))
    assert seeded.final_rel in names and seeded.old_error_rel in names
    assert seeded.conflict_rel in names


def test_options_off_leave_sections_out(seeded):
    out, manifest = seeded.build({k: False for k in diagnostics.OPTION_KEYS})
    names = set(_members(out))
    assert names == {"manifest.json"}
    assert manifest["files"] == []


def test_symlink_out_of_the_data_dir_is_not_followed(seeded, tmp_path):
    outside = tmp_path / "outside.CDF"
    outside.write_bytes(f"outside-{MARKER}".encode())
    link = seeded.data / "cdf" / "gc1" / "2026" / "09" / "LINK.CDF"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    out, _ = seeded.build({"all_cdfs": True})
    _assert_no_marker(_members(out))


def test_a_cdf_holding_a_secret_is_skipped(seeded):
    p = seeded.data / seeded.problem_rels[0]
    p.write_bytes(b"CDF" + f"code-{MARKER}".encode())
    out, manifest = seeded.build()
    members = _members(out)
    _assert_no_marker(members)
    assert seeded.problem_rels[0] not in members
    assert any(s["name"] == seeded.problem_rels[0] and "secret" in s["reason"]
               for s in manifest["skipped"])


def test_second_concurrent_build_is_refused(seeded):
    with diagnostics.exclusive():
        with pytest.raises(diagnostics.Busy):
            with diagnostics.exclusive():
                pass
    with diagnostics.exclusive():      # released again
        pass


def test_estimate(seeded):
    est = diagnostics.estimate(data_dir=seeded.data, db=seeded.db)
    assert list(est) == list(diagnostics.OPTION_KEYS)
    for key, e in est.items():
        assert {"label", "default", "bytes", "files"} <= set(e), key
        assert e["bytes"] >= 0
    assert est["all_cdfs"]["default"] is False
    assert est["all_cdfs"]["files"] == 9
    assert est["problem_cdfs"]["files"] == 7
    assert est["database"]["bytes"] >= seeded.db.stat().st_size


def test_status_snapshot_absent_and_present(seeded, monkeypatch):
    monkeypatch.setitem(sys.modules, "hub_control", None)     # import fails
    _, manifest = seeded.build()
    assert manifest["status_snapshot"] == {"available": False,
                                           "note": "status snapshot unavailable"}
    import types
    fake = types.ModuleType("hub_control")
    fake.status_snapshot = lambda: {"cpu_percent": 3.5, "rss_bytes": 1234}
    monkeypatch.setitem(sys.modules, "hub_control", fake)
    out, manifest = seeded.build()
    assert manifest["status_snapshot"] == {"available": True, "cpu_percent": 3.5,
                                           "rss_bytes": 1234}
    assert b"cpu_percent" in _members(out)["summary.txt"]


def test_runtime_state_reported(seeded):
    class T:
        def __init__(self, alive):
            self._alive = alive

        def is_alive(self):
            return self._alive

    class Exp(T):
        _last_error = {"gc1": "disk full"}
        _refused = {}

    class Maint(T):
        _failed_at = None

    rt = type("RT", (), {})()
    rt.worker, rt.exporter, rt.maintenance = T(True), Exp(False), Maint(True)
    _, manifest = seeded.build(runtime=rt)
    r = manifest["runtime"]
    assert r["running"] is True
    assert r["worker_alive"] is True and r["exporter_alive"] is False
    assert r["maintenance_alive"] is True
    assert r["exporter_last_errors"] == {"gc1": "disk full"}


def test_progress_is_reported(seeded):
    events = []
    seeded.build(progress=events.append)
    assert events and all("phase" in e for e in events)
