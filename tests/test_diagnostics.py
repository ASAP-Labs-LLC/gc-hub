"""The admin "Download diagnostics" bundle (diagnostics.build_bundle).

A hub layout is seeded the way ASAPSV1 has it (``<root>/gc/data`` is
GC_DATA_DIR, ``<root>/gc`` the app root with the updater's marker files,
``<root>/updater`` the updater) with every secret the hub or the updater keeps:
the admin password hash, agent token hashes, the setup code, the QBench
credential store, the Selenium login file on the share
(``qbench_pdf_uploader.CREDENTIALS_FILE``), secret-looking settings.json keys
and the updater's GitHub token, each carrying the same distinctive MARKER, plus
logs that mention them the way the hub and the updater really log. The bundle
must never contain MARKER anywhere: not in a zip member or its name, not in the
database copy's bytes, in any case and in escaped form.

The critic's attacks on the first version are regression tests here: the
export-path file read (C1), the Selenium login on the share (I1), hash
fragments corrupting data (I2), unknown/escaped/case-varied secrets (I3), disk
safety (I4), UTF-16 logs and secret file names (M2), symlink escapes (M5).
"""
from __future__ import annotations

import json
import os
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
import exports  # noqa: E402
import store  # noqa: E402

MARKER = "ZQXSEKRIT7f3a"
GHP = "ghp_" + "Ab1" * 12                     # a GitHub PAT shape, not a known value


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class Seeded:
    """A hub layout with samples, CDFs, exports, logs, reports and secrets."""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.app_root = root / "gc"
        self.data = self.app_root / "data"
        self.data.mkdir(parents=True)
        self.home = root / "home"
        self.home.mkdir()
        self.share = root / "share"
        self.share.mkdir()
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
        # the Selenium login where production keeps it (the share), and a copy
        # in the data folder (the fallback)
        self.creds = self.share / "qbenchlogin.txt"
        self.creds.write_text(f"labuser\nSelPw-{MARKER}\n", encoding="utf-8")
        import qbench_pdf_uploader
        monkeypatch.setattr(qbench_pdf_uploader, "CREDENTIALS_FILE", str(self.creds))
        (self.data / "qbenchlogin.txt").write_text(f"user-{MARKER}\npw-{MARKER}\n",
                                                   encoding="utf-8")
        qstore = {"client_id": f"cid-{MARKER}", "client_secret": f"csec-{MARKER}"}
        (self.data / "qbench.json").write_text(json.dumps(qstore), encoding="utf-8")
        monkeypatch.setenv("QBENCH_STORE_PATH", str(self.data / "qbench.json"))
        monkeypatch.setenv("QBENCH_CLIENT_ID", f"envcid-{MARKER}")
        (self.home / "qbench.json").write_text(json.dumps(qstore), encoding="utf-8")
        (self.data / "qbench.json.corrupt-20260101-000000").write_text(
            json.dumps(qstore), encoding="utf-8")
        self.corrections = self.share / "correction_factors.json"
        self.corrections.write_text(json.dumps({"gc1": {"IBP": 1.5, "FBP": -2.0}}),
                                    encoding="utf-8")
        self.settings = {
            "processed_cdf_dir": str(self.data / "processed_cdf"),
            "correction_factors_json": str(self.corrections),
            "export_folder": str(self.data / "exports"),
            "qbench_password": f"pw-{MARKER}",
            "nested": {"client_secret": f"ns-{MARKER}", "folder": "C:/x"},
            "analysis_window": "301",
        }
        (self.data / "settings.json").write_text(json.dumps(self.settings), encoding="utf-8")

        # ── logs (the setup code is logged at start-up, as admin_auth does) ──
        (self.data / "app.log").write_text(
            "2026-09-29 [INFO] webapp: started\n"
            f"2026-09-29 [WARNING] admin_auth: No admin password is set. Admin setup code: "
            f"code-{MARKER} (also in x).\n"
            "2026-09-29 [ERROR] hub: something broke\n"
            "2026-09-29 [INFO] distill: area 1310000 counts, method pbkdf2_sha256 unrelated\n",
            encoding="utf-8")
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
        self.error_id = self.ids[("error", recent, None)][0]
        store.samples.update(self.error_id, error="QBench said: Bearer abcdefghijklmnop123 bad",
                             db=self.db)
        self.final_rel = "cdf/gc1/2026/09/S0.CDF"
        self.noted_rel = "cdf/gc1/2026/09/S6.CDF"
        self.old_error_rel = "cdf/gc1/2026/09/S7.CDF"
        self.problem_rels = [f"cdf/gc1/2026/09/S{n}.CDF" for n in (1, 2, 3, 4, 5, 6)]
        self.conflict_rel = cdf("cdf/gc1/conflicts/2026/09/C1.CDF")
        store.conflicts.add("gc1", "L1", "2026-09-21T10:00:01", None, "shaC", self.conflict_rel,
                            received_at=recent, db=self.db)

        # ── exports: gc1 at the default path, with its sidecar and 30 rows ──
        self.export = self.data / "results" / "gc1_results.csv"
        self.export.parent.mkdir(parents=True)
        self.export.write_text(exports.header_line() + "".join(f"row{i}\r\n" for i in range(30)),
                               encoding="utf-8", newline="")
        exports.sidecar_path(self.export).write_text(
            json.dumps({"instrument": "gc1", "size": 1, "seq": 0, "sha256": "x"}),
            encoding="utf-8")

        # ── calibration: gc1's CDF outside the data folder; gc2 names a non-CDF ──
        self.cal = root / "src" / "CAL_09162026.CDF"
        self.cal.parent.mkdir()
        self.cal.write_bytes(b"CDF\x01calibration")
        store.instruments.upsert({"id": "gc1", "calibration_cdf": str(self.cal),
                                  "calibration_assignments": json.dumps(
                                      [{"time": 1.0, "carbon": 5}])}, db=self.db)
        store.instruments.upsert({"id": "gc2", "calibration_cdf": str(self.creds)}, db=self.db)
        std = self.data / "gc_comparison_standards"
        std.mkdir()
        (std / "Diesel.CDF").write_bytes(b"CDF\x01standard")

        # ── reports: 25, of which the newest 20 go in ──
        rep = self.data / "reports"
        rep.mkdir()
        for i in range(25):
            p = rep / f"parity-{i:02d}.md"
            p.write_text(f"report {i}\n", encoding="utf-8")
            os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))

        # ── updater files in the data folder ──
        self.staged = self.data / "staged.json"
        self.staged.write_text(json.dumps({"tag": "v3.0.1", "healthy": True}), encoding="utf-8")
        (self.data / "switch-refused").write_text("reason: busy", encoding="utf-8")

        # ── the app root's markers and the updater (sibling of the app root) ──
        (self.data / "paused").write_text("paused by ryan", encoding="utf-8")
        (self.app_root / "VERSION").write_text("v3.0.0\n", encoding="utf-8")
        # things that must never be read from an app root (N1)
        (self.app_root / ".netrc").write_text(f"machine github.com password NETRC{MARKER}\n")
        (self.app_root / "id_ed25519").write_text(f"-----BEGIN KEY-----\nKEY{MARKER}\n")
        (self.app_root / ".env").write_text(f"DB_URL=postgres://u:DBPW{MARKER}@h/db\n")
        (self.app_root / "notes.txt").write_text("NOTESCONTENTxyz\n")
        (self.app_root / "releases" / "v3.0.0").mkdir(parents=True)
        (self.app_root / "current.txt").write_text("v3.0.0", encoding="utf-8")
        self.updater = root / "updater"
        self.updater.mkdir()
        lines = [f"2026-09-29 updater line {i}" for i in range(600)]
        lines[-3] = f"2026-09-29 using token {GHP} for github"
        lines[-2] = f"2026-09-29 auth header Bearer {MARKER}-bearer"
        (self.updater / "updater.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (self.updater / "config.json").write_text(json.dumps(
            {"github_token": f"ghp_{MARKER}xyz", "repo": "ASAP-Labs-LLC/gc-hub",
             "interval": 20, "apps": [{"name": "gc", "access_token": f"at-{MARKER}"}]}),
            encoding="utf-8")
        (self.updater / "config.example.json").write_text('{"x": 1}', encoding="utf-8")
        (self.updater / "secrets.json").write_text(f'{{"k": "UPDSECRET{MARKER}"}}',
                                                   encoding="utf-8")
        (self.updater / ".env").write_text(f"GH=UPDENV{MARKER}\n", encoding="utf-8")

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
    monkeypatch.delenv("GC_UPDATER_DIR", raising=False)
    monkeypatch.delenv("GC_APP_ROOT", raising=False)
    diagnostics.clear_estimate_cache()
    return Seeded(tmp_path, monkeypatch)


def _members(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def _hits(members: dict, needle: str) -> list:
    """Members holding ``needle`` in any case, UTF-8 or UTF-16, raw or JSON-escaped."""
    forms = {needle, json.dumps(needle)[1:-1]}
    out = []
    for name, data in members.items():
        low = data.lower()
        for f in forms:
            if (f.lower().encode() in low or f.lower().encode("utf-16-le") in low
                    or f.lower() in name.lower()):
                out.append(name)
                break
    return out


def _assert_no_marker(members: dict):
    assert _hits(members, MARKER) == []


ALL_ON = {k: True for k in diagnostics.OPTION_KEYS}


# ── secrets never go in ─────────────────────────────────────────────────────

def test_marker_appears_nowhere_with_every_option_on(seeded):
    out, manifest = seeded.build(ALL_ON)
    members = _members(out)
    _assert_no_marker(members)
    names = set(members)
    for banned in ("admin-setup-code.txt", "qbenchlogin.txt", "qbench.json"):
        assert not any(n.rsplit("/", 1)[-1].startswith(banned) for n in names), banned
    assert "database/gc.db" in names


def test_selenium_login_on_the_share_is_redacted(seeded):
    """I1: the production file is qbench_pdf_uploader.CREDENTIALS_FILE (the
    share), not the data folder; its password is redacted from the logs."""
    (seeded.data / "qbenchlogin.txt").unlink()
    with open(seeded.data / "app.log", "a", encoding="utf-8") as f:
        f.write(f"2026 [WARNING] qbench: login failed for labuser/SelPw-{MARKER}\n")
    out, manifest = seeded.build(ALL_ON)
    members = _members(out)
    assert _hits(members, f"SelPw-{MARKER}") == []
    assert manifest["secrets"]["selenium_login"] == "read"


def test_an_unreadable_selenium_login_is_noted(seeded, monkeypatch):
    """A hanging share is given up on (bounded) and said so in the summary."""
    monkeypatch.setattr(diagnostics, "SHARE_TIMEOUT_SECONDS", 0.2)
    real = diagnostics._read_text

    def slow(path, *a, **kw):
        if str(path) == str(seeded.creds):
            threading.Event().wait(2)
        return real(path, *a, **kw)

    monkeypatch.setattr(diagnostics, "_read_text", slow)
    out, manifest = seeded.build()
    assert manifest["secrets"]["selenium_login"] == "creds file unreadable"
    assert b"creds file unreadable" in _members(out)["summary.txt"]


def test_the_typed_admin_password_is_redacted(seeded):
    """I3: the plaintext password (the route passes the one it was given)."""
    plain = "Hunter2PlainAdminPw!"
    sid = seeded.error_id
    store.samples.update(sid, review_note=f"pw is {plain}", db=seeded.db)
    with open(seeded.data / "app.log", "a", encoding="utf-8") as f:
        f.write(f"2026 [INFO] webapp: typed {plain} into the lab id box\n")
    out, _ = seeded.build(ALL_ON, extra_secrets=[plain])
    assert _hits(_members(out), plain) == []


def test_escaped_and_case_varied_secrets_are_redacted(seeded):
    """I3: a secret with a backslash and a quote, surfaced in a JSON member,
    and an upper-cased token hash in a log."""
    bs = 'Qb\\sec"ret99'
    q = json.loads((seeded.data / "qbench.json").read_text())
    q["client_secret"] = bs
    (seeded.data / "qbench.json").write_text(json.dumps(q))
    nf = json.loads((seeded.data / "notifications.json").read_text())
    nf.append({"id": "n2", "level": "error", "message": f"auth failed with {bs}"})
    (seeded.data / "notifications.json").write_text(json.dumps(nf))
    with open(seeded.data / "app.log", "a", encoding="utf-8") as f:
        f.write(f"hash TOKHASH-{MARKER.upper()}-1\n")
    out, _ = seeded.build(ALL_ON)
    members = _members(out)
    assert _hits(members, bs) == []
    assert _hits(members, MARKER.upper()) == []


def test_secret_shapes_are_redacted_by_pattern(seeded):
    """I3: GitHub tokens, bearer tokens, JSON token fields and the setup-code
    log line, none of them a value the hub knows."""
    pat = "github_pat_" + "x1_" * 10
    with open(seeded.data / "app.log", "a", encoding="utf-8") as f:
        f.write(f"push with {GHP} and {pat}\n")
        f.write("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.unknownbearer\n")
        f.write('{"access_token": "unknownjsontoken123", "password": "pw-unknown-777", '
                '"other": "kept-value"}\n')
        f.write("No admin password is set. Admin setup code: unknownSetupCode9 (also in y).\n")
    out, _ = seeded.build()
    log = _members(out)["logs/app.log"].decode()
    for gone in (GHP, pat, "unknownbearer", "unknownjsontoken123", "pw-unknown-777",
                 "unknownSetupCode9"):
        assert gone not in log, gone
    assert "kept-value" in log and "[REDACTED]" in log


def test_short_secrets_are_counted_not_redacted(seeded):
    """I3: a secret too short to redact safely is left alone but counted
    (never shown) in the summary."""
    store.settings_kv.set("tiny_token", "ab1", db=seeded.db)
    out, manifest = seeded.build()
    assert manifest["secrets"]["too_short_to_redact"] >= 1
    summary = _members(out)["summary.txt"].decode()
    assert "too short to redact" in summary and "ab1" not in summary


def test_hash_fragments_do_not_corrupt_data(seeded, tmp_path):
    """I2: the admin hash's iteration count and algorithm name are not
    secrets; only its salt and digest are redacted."""
    store.samples.update(seeded.error_id, review_note="area 1310000 counts", db=seeded.db)
    out, _ = seeded.build()
    members = _members(out)
    log = members["logs/app.log"].decode()
    assert "area 1310000 counts" in log and "pbkdf2_sha256 unrelated" in log
    copy = tmp_path / "copy.db"
    copy.write_bytes(members["database/gc.db"])
    conn = sqlite3.connect(copy)
    try:
        note = conn.execute("SELECT review_note FROM samples WHERE id=?",
                            (seeded.error_id,)).fetchone()[0]
    finally:
        conn.close()
    assert note == "area 1310000 counts"
    secrets = diagnostics.known_secrets(seeded.data, seeded.db).values
    assert "310000" not in secrets and "pbkdf2_sha256" not in secrets
    assert f"{MARKER}salt" in secrets and f"{MARKER}hash" in secrets


def test_deleted_rows_do_not_survive_in_the_copy(seeded):
    old = "OLDHASHdeleted" + "x" * 40
    store.settings_kv.set("note_blob", old * 50, db=seeded.db)
    store.settings_kv.delete("note_blob", db=seeded.db)
    out, _ = seeded.build()
    assert old.encode() not in _members(out)["database/gc.db"]


def test_a_secret_in_a_file_name_skips_that_member_only(seeded):
    """M2: skipped and noted, the bundle still built."""
    p = seeded.data / "cdf" / "gc1" / "2026" / "09" / f"X-tokhash-{MARKER}-1.CDF"
    p.write_bytes(b"CDF")
    out, manifest = seeded.build({"all_cdfs": True})
    members = _members(out)
    _assert_no_marker(members)
    assert seeded.final_rel in members
    assert any("secret" in s["reason"] for s in manifest["skipped"])


def test_a_utf16_log_holding_a_secret_is_skipped(seeded):
    """M2: redaction works on UTF-8; a UTF-16 member that still holds a secret
    is left out and noted, not the whole bundle refused."""
    (seeded.data / "agent.log").write_bytes(f"code-{MARKER}\r\n".encode("utf-16-le"))
    out, manifest = seeded.build()
    members = _members(out)
    _assert_no_marker(members)
    assert "logs/agent.log" not in members and "logs/app.log" in members
    assert any(s["name"] == "logs/agent.log" and "secret" in s["reason"]
               for s in manifest["skipped"])
    assert b"logs/agent.log" in members["summary.txt"]


def test_the_final_check_fails_closed(seeded, monkeypatch):
    """With redaction and the per-member check both disabled, the final pass
    over the zip refuses it and leaves no file."""
    monkeypatch.setattr(diagnostics, "redact_text", lambda text, secrets: text)
    monkeypatch.setattr(diagnostics._Bundle, "leaks", lambda self, name, data: False)
    out = seeded.root / "g.zip"
    with pytest.raises(diagnostics.SecretLeak):
        diagnostics.build_bundle({}, data_dir=seeded.data, db=seeded.db, out_path=out)
    assert not out.exists()
    assert not list(seeded.root.glob("*.work"))


def test_a_cdf_holding_a_secret_is_skipped(seeded):
    p = seeded.data / seeded.problem_rels[0]
    p.write_bytes(b"CDF" + f"code-{MARKER}".encode())
    out, manifest = seeded.build()
    members = _members(out)
    _assert_no_marker(members)
    assert seeded.problem_rels[0] not in members
    assert any(s["name"] == seeded.problem_rels[0] and "secret" in s["reason"]
               for s in manifest["skipped"])


# ── C1: the export file read ────────────────────────────────────────────────

def _health(out) -> dict:
    return {r["instrument"]: r for r in json.loads(_members(out)["exports/health.json"])}


def test_export_path_naming_a_secret_file_is_never_read(seeded, tmp_path):
    updater_json = tmp_path / "updater.json"
    updater_json.write_text(f'{{"github_pat": "ghp_{MARKER}PAT0000000000000000000"}}\n')
    with store.connection(seeded.db) as conn:     # bypass the (now .csv-only) admin route
        conn.execute("UPDATE instruments SET export_path=? WHERE id='gc2'", (str(seeded.creds),))
    store.instruments.upsert({"id": "gc3", "name": "x"}, db=seeded.db)
    with store.connection(seeded.db) as conn:
        conn.execute("UPDATE instruments SET export_path=? WHERE id='gc3'", (str(updater_json),))
    out, _ = seeded.build({"exports": True})
    h = _health(out)
    for inst in ("gc2", "gc3"):
        assert h[inst]["tail"] == [] and h[inst]["sidecar"] is None, h[inst]
        assert "not a .csv" in h[inst]["tail_skipped"]
    _assert_no_marker(_members(out))
    assert "SelPw" not in json.dumps(h)


def test_a_csv_without_sidecar_or_header_is_not_read(seeded, tmp_path):
    other = tmp_path / "random.csv"
    other.write_text(f"secret,{MARKER}-csv\nmore,stuff\n", encoding="utf-8")
    with store.connection(seeded.db) as conn:
        conn.execute("UPDATE instruments SET export_path=? WHERE id='gc2'", (str(other),))
    out, _ = seeded.build({"exports": True})
    g = _health(out)["gc2"]
    assert g["tail"] == [] and g["size"] == other.stat().st_size and g["mtime"]
    assert "not a hub export" in g["tail_skipped"]


def test_a_csv_symlink_to_another_file_is_not_read(seeded, tmp_path):
    target = tmp_path / "outside-secret.txt"
    target.write_text("OUTSIDEFILECONTENT\n" * 3)
    link = tmp_path / "innocent.csv"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    with store.connection(seeded.db) as conn:
        conn.execute("UPDATE instruments SET export_path=? WHERE id='gc2'", (str(link),))
    out, _ = seeded.build({"exports": True})
    assert _hits(_members(out), "OUTSIDEFILECONTENT") == []


def test_a_foreign_sidecar_does_not_open_the_tail(seeded, tmp_path):
    other = tmp_path / "foreign.csv"
    other.write_text("a,b\nc,d\n", encoding="utf-8")
    exports.sidecar_path(other).write_text(json.dumps({"instrument": "gc1"}), encoding="utf-8")
    with store.connection(seeded.db) as conn:
        conn.execute("UPDATE instruments SET export_path=? WHERE id='gc2'", (str(other),))
    out, _ = seeded.build({"exports": True})
    assert _health(out)["gc2"]["tail"] == []


def test_exports_health(seeded):
    with store.connection(seeded.db) as conn, store.write_txn(conn):
        conn.execute("INSERT INTO sample_results(sample_id, revision, results, reason, "
                     "processed_at) VALUES (1, 1, '{}', 'processed', 'x')")
        conn.execute("INSERT INTO export_rows(instrument_id, sample_id, revision, \"row\") "
                     "VALUES ('gc1', 1, 1, 'a,b')")
    out, _ = seeded.build()
    health = _health(out)
    gc1 = health["gc1"]
    assert Path(gc1["path"]) == seeded.export
    assert gc1["exists"] and gc1["size"] == seeded.export.stat().st_size
    assert gc1["sidecar"]["instrument"] == "gc1"
    assert gc1["lock_present"] is False
    assert gc1["pending"] == 1
    assert gc1["tail"] == [f"row{i}" for i in range(10, 30)]
    assert health["gc2"]["exists"] is False


def test_a_header_only_export_opens_the_tail_and_is_redacted(seeded):
    exports.sidecar_path(seeded.export).unlink()
    with open(seeded.export, "a", encoding="utf-8", newline="") as f:
        f.write(f"x,Bearer {MARKER}zz,y\r\n")
    out, _ = seeded.build()
    tail = _health(out)["gc1"]["tail"]
    assert tail and tail[-1].startswith("x,") and MARKER not in tail[-1]


# ── contents ────────────────────────────────────────────────────────────────

def test_manifest_and_summary(seeded):
    out, manifest = seeded.build()
    members = _members(out)
    doc = json.loads(members["manifest.json"])
    assert doc == json.loads(json.dumps(manifest))
    for key in ("app_version", "python", "platform", "disk", "uptime_seconds", "runtime",
                "updater", "created_at", "who", "options", "files", "status_snapshot",
                "secrets"):
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
                    "calibration": True, "updater": True, "environment": True,
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
              "settings_kv", "recent_errors"):
        assert f"tables/{t}.json" in m, t
    assert json.loads(m["tables/notifications.json"])[0]["message"] == "boom"
    kv = {r["key"]: r["value"] for r in json.loads(m["tables/settings_kv.json"])}
    assert "admin_password" not in kv and "some_api_token" not in kv
    assert kv["hub_url"] == "http://asapsv1:5560"


def test_recent_errors(seeded, monkeypatch):
    monkeypatch.setattr(diagnostics, "RECENT_ERRORS_LAST", 1)
    out, _ = seeded.build()
    errs = json.loads(_members(out)["tables/recent_errors.json"])
    assert len(errs) == 1
    assert errs[0]["id"] == seeded.error_id and errs[0]["status"] == "error"
    assert "abcdefghijklmnop123" not in errs[0]["error"] and "[REDACTED]" in errs[0]["error"]


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


def test_reports_newest_twenty(seeded):
    out, _ = seeded.build()
    names = sorted(n for n in _members(out) if n.startswith("reports/"))
    assert names == [f"reports/parity-{i:02d}.md" for i in range(5, 25)]


def test_problem_cdfs(seeded, monkeypatch):
    out, manifest = seeded.build()
    names = set(_members(out))
    for rel in seeded.problem_rels + [seeded.conflict_rel]:
        assert rel in names, rel
    assert seeded.final_rel not in names
    assert seeded.old_error_rel not in names
    monkeypatch.setattr(diagnostics, "PROBLEM_CDF_CAP_BYTES", 40)
    out, manifest = seeded.build()
    names = set(_members(out))
    included = [r for r in seeded.problem_rels + [seeded.conflict_rel] if r in names]
    assert 0 < len(included) < 7
    skipped = [s for s in manifest["skipped"] if s["reason"].startswith("problem CDF cap")]
    assert len(skipped) == 7 - len(included)


def test_problem_cdf_with_an_absolute_path(seeded, tmp_path):
    """I5: a CDF stored outside the data folder goes in when it is a .cdf and
    not a secret; a non-.cdf path never does."""
    outside = tmp_path / "elsewhere" / "ABS.CDF"
    outside.parent.mkdir()
    outside.write_bytes(b"CDF\x01abs")
    sid = seeded.error_id
    with store.connection(seeded.db) as conn:
        conn.execute("UPDATE samples SET cdf_path=? WHERE id=?", (str(outside), sid))
        conn.execute("UPDATE samples SET cdf_path=? WHERE id=?",
                     (str(seeded.creds), seeded.ids[("awaiting_calibration",
                                                     _iso(seeded.now - timedelta(days=2)),
                                                     None)][0]))
    out, manifest = seeded.build()
    members = _members(out)
    assert members.get("outside/ABS.CDF") == b"CDF\x01abs"
    assert not any("qbenchlogin" in n for n in members)
    _assert_no_marker(members)


def test_all_cdfs_off_by_default_on_when_asked(seeded):
    out, _ = seeded.build()
    assert seeded.final_rel not in _members(out)
    out, _ = seeded.build({"all_cdfs": True})
    names = set(_members(out))
    assert seeded.final_rel in names and seeded.old_error_rel in names
    assert seeded.conflict_rel in names


def test_all_cdfs_is_capped(seeded, monkeypatch):
    """I4: a hard cap on all raw CDFs; the truncation is in the summary."""
    monkeypatch.setattr(diagnostics, "ALL_CDF_CAP_BYTES", 50)
    out, manifest = seeded.build({"all_cdfs": True, "problem_cdfs": False})
    members = _members(out)
    cdfs = [n for n in members if n.startswith("cdf/")]
    assert 0 < len(cdfs) < 9
    assert sum(len(members[n]) for n in cdfs) <= 50
    assert manifest["truncated"]["all_cdfs"]
    assert b"all raw CDFs stopped at" in members["summary.txt"]


def test_options_off_leave_sections_out(seeded):
    out, manifest = seeded.build({k: False for k in diagnostics.OPTION_KEYS})
    assert set(_members(out)) == {"manifest.json"}
    assert manifest["files"] == []


def test_symlinks_out_of_the_data_dir_are_not_followed(seeded, tmp_path):
    """M5: a CDF or report symlinked to a file outside, and a directory loop."""
    outside = tmp_path / "outside.CDF"
    outside.write_bytes(b"OUTSIDEFILECONTENT")
    try:
        (seeded.data / "cdf" / "gc1" / "2026" / "09" / "LINK.CDF").symlink_to(outside)
        (seeded.data / "cdf" / "gc1" / "loop").symlink_to(seeded.data / "cdf")
        (seeded.data / "reports" / "zz-link.md").symlink_to(outside)
        (seeded.data / "cdf" / "outdir").symlink_to(tmp_path)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    out, _ = seeded.build({"all_cdfs": True})
    members = _members(out)
    assert _hits(members, "OUTSIDEFILECONTENT") == []
    assert not any("/loop/" in n or "/outdir/" in n for n in members)
    est = diagnostics.estimate(data_dir=seeded.data, db=seeded.db)
    assert est["all_cdfs"]["files"] == 9


def test_calibration_corrections_and_standards(seeded):
    out, manifest = seeded.build()
    m = _members(out)
    assert m["calibration/gc1/CAL_09162026.CDF"] == b"CDF\x01calibration"
    assert json.loads(m["calibration/gc1/assignments.json"]) == [{"time": 1.0, "carbon": 5}]
    assert not any(n.startswith("calibration/gc2/") and n.endswith(".txt") for n in m)
    assert json.loads(m["calibration/correction_factors.json"]) == {"gc1": {"IBP": 1.5,
                                                                            "FBP": -2.0}}
    listing = json.loads(m["calibration/standards.json"])
    assert listing == [{"name": "Diesel.CDF", "size": len(b"CDF\x01standard")}]
    assert m["calibration/standards/Diesel.CDF"] == b"CDF\x01standard"
    _assert_no_marker(m)


def test_standards_listed_only_when_large(seeded, monkeypatch):
    monkeypatch.setattr(diagnostics, "STANDARDS_MAX_BYTES", 5)
    out, _ = seeded.build()
    m = _members(out)
    assert "calibration/standards.json" in m
    assert not any(n.startswith("calibration/standards/") for n in m)


def test_updater_context(seeded):
    """I5: the updater log's tail, the app root's marker files and the
    updater's config, redacted; derived from GC_DATA_DIR's parent."""
    out, manifest = seeded.build()
    m = _members(out)
    log = m["updater/updater.log"].decode()
    lines = log.splitlines()
    assert len(lines) == diagnostics.UPDATER_LOG_LINES
    assert lines[-1] == "2026-09-29 updater line 599"
    assert GHP not in log and MARKER not in log and "[REDACTED]" in log
    cfg = json.loads(m["updater/config.json"])
    assert cfg["repo"] == "ASAP-Labs-LLC/gc-hub" and cfg["interval"] == 20
    assert cfg["github_token"] == "[REDACTED]"
    assert cfg["apps"][0]["access_token"] == "[REDACTED]"
    markers = json.loads(m["updater/app_root.json"])
    assert markers["files"]["current.txt"]["content"] == "v3.0.0"
    assert markers["files"]["VERSION"]["content"] == "v3.0.0\n"
    assert markers["releases"] == ["v3.0.0"]
    assert set(markers["files"]) == {"current.txt", "VERSION"}
    assert manifest["updater"]["paused"]["content"] == "paused by ryan"   # data dir marker
    # the updater folder: log + known config read; everything else by name/size only
    assert "updater/secrets.json" not in m and "updater/config.example.json" not in m
    listing = {f["name"]: f["size"] for f in json.loads(m["updater/listing.json"])}
    assert "secrets.json" in listing and "config.example.json" in listing
    assert ".env" not in listing
    _assert_no_marker(m)


def test_app_root_is_an_allowlist(seeded):
    """N1: dotfiles, keys and unknown files in the app root never go in, not
    even by name."""
    out, _ = seeded.build()
    m = _members(out)
    for bad in ("NETRC", "KEY" + MARKER, "DBPW", "NOTESCONTENTxyz", "id_ed25519", ".netrc",
                "notes.txt", ".env"):
        assert _hits({k: v for k, v in m.items() if k.startswith("updater/")}, bad) == [], bad
    _assert_no_marker(m)


def test_a_folder_that_is_not_an_app_root_is_not_read(seeded, tmp_path, monkeypatch):
    """N1: GC_APP_ROOT aimed at a home-like folder (no releases/, no current)."""
    home = tmp_path / "fakehome"
    home.mkdir()
    (home / ".netrc").write_text("machine github.com login ryan password NETRCPASSxyz\n")
    (home / ".git-credentials").write_text("https://ryan:GITCREDTOKENxyz@github.com\n")
    (home / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nKEYMATxyz\n")
    (home / ".env").write_text("DB_URL=postgres://u:DBPWxyz@h/db\n")
    (home / "VERSION").write_text("VERSIONFROMHOMExyz")
    monkeypatch.setenv("GC_APP_ROOT", str(home))
    out, _ = seeded.build({"updater": True})
    m = _members(out)
    for bad in ("NETRCPASSxyz", "GITCREDTOKENxyz", "KEYMATxyz", "DBPWxyz", "VERSIONFROMHOMExyz"):
        assert _hits(m, bad) == [], bad
    info = json.loads(m["updater/app_root.json"])
    assert "not an updater app root" in info["note"] and "files" not in info


def test_updater_paths_are_configurable_and_skipped_when_absent(seeded, tmp_path,
                                                                monkeypatch):
    monkeypatch.setenv("GC_UPDATER_DIR", str(tmp_path / "nope"))
    monkeypatch.setenv("GC_APP_ROOT", str(tmp_path / "nope2"))
    out, manifest = seeded.build()
    m = _members(out)
    assert not any(n.startswith("updater/") for n in m)
    assert not any(s["name"].startswith("updater/") for s in manifest["skipped"])


def test_environment(seeded):
    out, _ = seeded.build()
    env = json.loads(_members(out)["environment.json"])
    assert env["python"] and env["platform"]
    pkgs = env["packages"]
    assert any(p.startswith("pytest==") for p in pkgs)
    assert env["env"]["QBENCH_CLIENT_ID"] == "set"
    assert env["env"]["GC_DATA_DIR"] in ("set", "unset")
    assert env["env"]["PORT"] in ("set", "unset")
    _assert_no_marker(_members(out))


def test_status_snapshot_absent_and_present(seeded, monkeypatch):
    monkeypatch.setitem(sys.modules, "hub_control", None)
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


# ── one at a time, disk, estimate ───────────────────────────────────────────

def test_second_concurrent_build_is_refused(seeded):
    assert diagnostics.busy() is False
    with diagnostics.exclusive():
        assert diagnostics.busy() is True
        with pytest.raises(diagnostics.Busy):
            with diagnostics.exclusive():
                pass
    assert diagnostics.busy() is False
    with diagnostics.exclusive():
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


def test_estimate_is_cached(seeded, monkeypatch):
    """M5: the recursive walk runs at most once a minute."""
    calls = []
    real = diagnostics._walk_files

    def counting(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(diagnostics, "_walk_files", counting)
    diagnostics.estimate(data_dir=seeded.data, db=seeded.db)
    n = len(calls)
    diagnostics.estimate(data_dir=seeded.data, db=seeded.db)
    assert len(calls) == n
    diagnostics.clear_estimate_cache()
    diagnostics.estimate(data_dir=seeded.data, db=seeded.db)
    assert len(calls) == 2 * n


def test_disk_check(seeded, monkeypatch):
    """I4: refuse when the temp folder's disk has less free space than the
    estimate + the database + the margin."""
    import collections
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(diagnostics.shutil, "disk_usage",
                        lambda p: usage(10 ** 12, 0, diagnostics.DISK_MARGIN_BYTES + 10))
    with pytest.raises(diagnostics.NoSpace) as e:
        diagnostics.check_disk(seeded.data, diagnostics.normalize_options({}),
                               data_dir=seeded.data, db=seeded.db)
    assert "free" in str(e.value)
    monkeypatch.setattr(diagnostics.shutil, "disk_usage",
                        lambda p: usage(10 ** 12, 0, 10 ** 12))
    diagnostics.check_disk(seeded.data, diagnostics.normalize_options({}),
                           data_dir=seeded.data, db=seeded.db)


def test_cleanup_at_startup_removes_everything(seeded):
    tmp = seeded.data / diagnostics.TMP_DIRNAME
    tmp.mkdir()
    (tmp / "x.zip.part").write_bytes(b"x")
    (tmp / "y.zip.part.work").mkdir()
    diagnostics.cleanup_tmp(seeded.data, max_age=0)
    assert list(tmp.iterdir()) == []


def test_bounded_gives_up_on_a_hanging_call():
    ok, value = diagnostics.bounded(lambda: 5, timeout=1)
    assert (ok, value) == (True, 5)
    ok, value = diagnostics.bounded(lambda: threading.Event().wait(3), timeout=0.1)
    assert ok is False


def test_app_and_uploader_name_the_same_selenium_login_file():
    import ast
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    consts = {n.targets[0].id: n.value.value for n in ast.parse(src).body
              if isinstance(n, ast.Assign) and len(n.targets) == 1
              and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Constant)}
    import qbench_pdf_uploader
    assert consts["_QBENCH_CREDS_FILE"] == qbench_pdf_uploader.CREDENTIALS_FILE


# ── round 3 ─────────────────────────────────────────────────────────────────

def test_more_token_shapes_are_redacted(seeded):
    with open(seeded.data / "app.log", "a", encoding="utf-8") as f:
        f.write("oauth gho_" + "Q" * 30 + " and ghs_" + "R" * 30 + " and ghu_" + "S" * 25 + "\n")
        f.write("clone https://ryan:URLPASSWORD123@github.com/x.git\n")
        f.write("Authorization: token AUTHTOKENabc123\n")
        f.write("Authorization: Basic QkFTSUNBVVRIeHl6\n")
        f.write('{"githubPat": "CAMELPATxyz1", "export_path": "C:/keep/me.csv"}\n')
        f.write("githubPat = INIPATxyz2\n")
        f.write("login pwd=PWDVALUExyz3 path=C:/also/kept\n")
    out, _ = seeded.build()
    log = _members(out)["logs/app.log"].decode()
    for gone in ("Q" * 30, "R" * 30, "S" * 25, "URLPASSWORD123", "AUTHTOKENabc123",
                 "QkFTSUNBVVRIeHl6", "CAMELPATxyz1", "INIPATxyz2", "PWDVALUExyz3"):
        assert gone not in log, gone
    assert "https://ryan:[REDACTED]@github.com" in log
    assert "C:/keep/me.csv" in log and "path=C:/also/kept" in log


def test_secret_key_names():
    for k in ("githubPat", "github_pat", "PAT", "authToken", "Authorization", "pwd", "db_passwd",
              "clientSecret", "apiKey", "api-key", "credentials", "password"):
        assert diagnostics.is_secret_key(k), k
    for k in ("export_path", "path", "processed_cdf_dir", "author", "pattern", "patch",
              "analysis_window", "hub_url", "compatible"):
        assert not diagnostics.is_secret_key(k), k


def test_settings_keep_paths_redact_camel_case_secrets(seeded):
    doc = json.loads((seeded.data / "settings.json").read_text())
    doc["githubPat"] = "CAMEL-SETTING-PAT-1"
    (seeded.data / "settings.json").write_text(json.dumps(doc))
    out, _ = seeded.build()
    s = json.loads(_members(out)["settings/settings.json"])
    assert s["githubPat"] == "[REDACTED]"
    assert s["processed_cdf_dir"] == seeded.settings["processed_cdf_dir"]


def test_outside_cdfs_need_netcdf_magic_and_a_size_cap(seeded, tmp_path, monkeypatch):
    fake = tmp_path / "fake" / "NOTREALLY.CDF"
    fake.parent.mkdir()
    fake.write_text("machine x password FAKECDFxyz\n")
    big = tmp_path / "fake" / "BIG.CDF"
    big.write_bytes(b"\x89HDF\r\n" + b"0" * 200)
    link = tmp_path / "fake" / "LINK.CDF"
    try:
        link.symlink_to(seeded.creds)
    except (OSError, NotImplementedError):
        link = None
    with store.connection(seeded.db) as conn:
        conn.execute("UPDATE instruments SET calibration_cdf=? WHERE id='gc2'", (str(fake),))
        conn.execute("UPDATE samples SET cdf_path=? WHERE id=?", (str(big), seeded.error_id))
        if link is not None:
            recent = _iso(seeded.now - timedelta(days=2))
            sid = seeded.ids[("awaiting_calibration", recent, None)][0]
            conn.execute("UPDATE samples SET cdf_path=? WHERE id=?", (str(link), sid))
    monkeypatch.setattr(diagnostics, "CDF_FILE_MAX_BYTES", 100)
    out, manifest = seeded.build()
    m = _members(out)
    assert _hits(m, "FAKECDFxyz") == []
    assert not any(n.endswith("NOTREALLY.CDF") for n in m)
    assert not any(n.endswith("BIG.CDF") for n in m)
    assert not any(n.endswith("LINK.CDF") for n in m)
    reasons = " ".join(s["reason"] for s in manifest["skipped"])
    assert "not a netCDF" in reasons and "too large" in reasons
    assert b"NOTREALLY.CDF" in m["summary.txt"]
    assert m["calibration/gc1/CAL_09162026.CDF"] == b"CDF\x01calibration"


def test_numeric_known_secrets_are_redacted(seeded):
    store.samples.update(seeded.error_id, review_note="pw 83920175", db=seeded.db)
    q = json.loads((seeded.data / "qbench.json").read_text())
    q["client_secret"] = "55512345"
    (seeded.data / "qbench.json").write_text(json.dumps(q))
    seeded.creds.write_text("labuser\n90807060\n", encoding="utf-8")
    with open(seeded.data / "app.log", "a", encoding="utf-8") as f:
        f.write("typed 83920175, secret 55512345, web 90807060, user labuser, area 1310000\n")
    out, manifest = seeded.build(extra_secrets=["83920175"])
    m = _members(out)
    for gone in ("83920175", "55512345", "90807060"):
        assert _hits(m, gone) == [], gone
    log = m["logs/app.log"].decode()
    assert "labuser" in log and "1310000" in log      # the username and data survive


# ── round 4: fewer false positives, the remaining false negatives ───────────

R = diagnostics.Secrets([])


def test_key_names_are_whole_words():
    strong = ("apiKey", "API_KEY", "client-secret", "accessToken", "refresh_token", "sessionid",
              "sessionId", "SessionID", "x-api-key", "authToken", "dbPassword", "PASSWORD",
              "clientSecret", "private_key", "passwd", "credentials", "secret")
    for k in strong:
        assert diagnostics.is_secret_key(k, structured=False), k
    weak = ("pwd", "Pwd", "pass", "session", "pat", "githubPat", "pw", "auth", "creds")
    for k in weak:
        assert diagnostics.is_secret_key(k, structured=True), k
        assert not diagnostics.is_secret_key(k, structured=False), k
    never = ("export_path", "author", "author_ip", "path", "patch", "pattern", "keyboard",
             "monkey", "passes", "bypass", "token_issued_at", "has_token", "session_count",
             "authority", "compass", "sample_key", "key", "key_id", "auth_status",
             "pw_changed_at", "spatial", "passage", "private_notes", "secretary")
    for k in never:
        assert not diagnostics.is_secret_key(k, structured=True), k


@pytest.mark.parametrize("line", [
    "2026-09-29 10:00:00,123 [WARNING] admin_auth: wrong admin password from 10.0.0.5",
    "2026-09-29 10:00:00,123 [INFO] requests.auth: retrying with digest",
    "2026-09-29 10:00:00,123 [INFO] qbench_secrets: store not found",
    "pass: 3 of 5 passed", "session: 4 samples", "status: final, auth: ok", "pw: 12",
    "pat: 7", "key: T50", "path=cdf/gc1/2026/09/S1.CDF", "pattern=D2887", "patch: v2.1",
    "export_path: C:\\share\\gc1.csv", "author: RB", "Best Fit: Diesel #2, Fit Score: 0.93",
    "Lab ID: 40304", "hub: auth refused for gc1: bad token", "token revoked for gc1",
    "the secretary: Jane", "private_notes: see binder 4", "password: incorrect",
])
def test_prose_and_lab_data_survive(line):
    assert R.redact(line) == line


@pytest.mark.parametrize("line,gone", [
    ("apiKey=AK1xxxxxxx", "AK1xxxxxxx"), ("API_KEY: AK2xxxxxxx", "AK2xxxxxxx"),
    ("client-secret=CS1xxxxxxx", "CS1xxxxxxx"), ('"accessToken": "AT1xxxxxxx"', "AT1xxxxxxx"),
    ("refresh_token=RT1xxxxxxx", "RT1xxxxxxx"), ("sessionid=SID1xxxxxx", "SID1xxxxxx"),
    ("sessionid: SID2xxxxxx", "SID2xxxxxx"), ("pwd=PWD1xxxxxx", "PWD1xxxxxx"),
    ('password="QUOTED1xxxx"', "QUOTED1xxxx"), ("password = 'QUOTED2xxxx'", "QUOTED2xxxx"),
    ('password: "QUOTED3xxxx"', "QUOTED3xxxx"), ("passwd  =  SPACED1xxxx", "SPACED1xxxx"),
    ('{"password":"JSON1xxxxx"}', "JSON1xxxxx"), ("{'password': 'PYREPR1xxxx'}", "PYREPR1xxxx"),
    ("{'pat': 'PYREPR2xxxx'}", "PYREPR2xxxx"),
    ("https://api.x/?access_token=QS1xxxxxxx&x=1", "QS1xxxxxxx"),
    ("Cookie: sessionid=CK1xxxxxxx; path=/", "CK1xxxxxxx"),
    ("Set-Cookie: session=CK2xxxxxxx", "CK2xxxxxxx"),
    ("Authorization: Basic QkFTSUMxeHh4eA==", "QkFTSUMxeHh4eA=="),
    ("  pass = INIPASSxyz", "INIPASSxyz"),
    ("private_key = PKVALUExyz1", "PKVALUExyz1"),
    ("-----BEGIN RSA PRIVATE KEY----- MIIEpemBODYxxxxxxx -----END RSA PRIVATE KEY-----",
     "MIIEpemBODYxxxxxxx"),
    ("private_key: -----BEGIN PRIVATE KEY-----\nPEMLINE2xxxxxx\nMORELINE3xxxx\n"
     "-----END PRIVATE KEY-----\nafter", "PEMLINE2xxxxxx"),
])
def test_secret_forms_are_redacted(line, gone):
    out = R.redact(line)
    assert gone not in out and "[REDACTED]" in out, out


def test_redaction_keeps_the_shape():
    assert R.redact("Authorization: Basic QkFTSUMxeHh4eA==") == "Authorization: Basic [REDACTED]"
    assert R.redact('password="QUOTED1xxxx"') == 'password="[REDACTED]"'


def test_pem_block_is_redacted_whole():
    text = ("before\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA1\nBBBB2\n"
            "-----END OPENSSH PRIVATE KEY-----\nafter line\n")
    out = R.redact(text)
    assert "AAAA1" not in out and "BBBB2" not in out
    assert out.startswith("before\n") and "after line" in out


def test_the_separator_does_not_cross_newlines():
    text = "password:\nnext line stays 1234\ntoken=\nalso stays 5678\n"
    assert R.redact(text) == text


def test_environment_json_says_set_not_redacted(seeded):
    out, _ = seeded.build()
    env = json.loads(_members(out)["environment.json"])["env"]
    assert env["QBENCH_CLIENT_SECRET"] == "set" and env["QBENCH_CLIENT_ID"] == "set"


def test_logger_names_survive_in_the_bundle(seeded):
    with open(seeded.data / "app.log", "a", encoding="utf-8") as f:
        f.write("2026-09-29 10:00:00,123 [WARNING] admin_auth: wrong admin password "
                "from 10.0.0.5\n")
    out, _ = seeded.build()
    log = _members(out)["logs/app.log"].decode()
    assert "admin_auth: wrong admin password from 10.0.0.5" in log


def test_lab_data_survives_the_database_copy(seeded, tmp_path):
    notes = {
        seeded.error_id: "pass: 3 of 5, area 1310000, session: 4, auth: ok",
    }
    other = seeded.ids[("final", _iso(seeded.now - timedelta(days=2)), "check this one")][0]
    notes[other] = '{"author": "RB", "export_path": "C:/share/gc1.csv", "key": "T50"}'
    for sid, note in notes.items():
        store.samples.update(sid, review_note=note, db=seeded.db)
    # (the seeded bearer token in this sample's error is meant to be redacted)
    store.samples.update(seeded.error_id, error="calibration failed at C12", db=seeded.db)
    with store.connection(seeded.db) as conn:
        conn.execute("UPDATE instruments SET export_path='C:/share/gc2.csv' WHERE id='gc2'")
    live = sqlite3.connect(seeded.db)
    try:
        before = {t: live.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall()
                  for t in ("samples", "conflicts", "sample_results", "export_rows",
                            "import_runs", "report_log", "comment_presets")}
    finally:
        live.close()
    out, _ = seeded.build()
    copy = tmp_path / "copy.db"
    copy.write_bytes(_members(out)["database/gc.db"])
    conn = sqlite3.connect(copy)
    try:
        for t, rows in before.items():
            assert conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() == rows, t
        assert conn.execute("SELECT export_path FROM instruments WHERE id='gc2'"
                            ).fetchone()[0] == "C:/share/gc2.csv"
    finally:
        conn.close()


# ── integration with hub_control (the tray lane) ───────────────────────────

def test_status_snapshot_comes_from_hub_control(seeded):
    import hub_control
    hub_control.reset()
    try:
        out, manifest = seeded.build()
    finally:
        hub_control.reset()
    snap = manifest["status_snapshot"]
    assert snap["available"] is True, snap
    for key in ("cpu_percent", "rss_bytes", "uptime_seconds", "processing_paused",
                "updater_paused", "queue"):
        assert key in snap, key
    assert b"processing_paused" in _members(out)["summary.txt"]


def test_hub_control_stop_waits_for_a_diagnostics_bundle(tmp_path):
    import hub_control
    reason = "a diagnostics bundle is being built"
    hub_control.reset()
    try:
        assert reason not in hub_control.busy_reasons()
        with diagnostics.exclusive():
            assert reason in hub_control.busy_reasons()
        waiting = tmp_path / "b.zip"
        waiting.write_bytes(b"x")
        token = diagnostics.register_download(waiting, "b.zip")
        assert reason in hub_control.busy_reasons()          # waiting to be downloaded
        diagnostics.claim_download(token)
        assert reason not in hub_control.busy_reasons()
    finally:
        hub_control.reset()


# ── the sign-in lane (auth/login): web_sessions and new env vars ────────────

def test_web_sessions_keep_their_rows_with_token_hash_nulled_in_the_copy(seeded, tmp_path):
    """Spec D7: the DB copy keeps web_sessions (name, method, IP, times) with
    every token_hash nulled (the schema-v3 table, as the hub writes it)."""
    with store.connection(seeded.db) as conn:
        assert "web_sessions" in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    store.web_sessions.add(f"sess-{MARKER}-hash", name="Ryan C", method="card",
                           ip="203.0.113.9", user_agent="UA", expires_at="2099-01-01T00:00:00+00:00",
                           db=seeded.db)
    store.web_sessions.add(f"sess-{MARKER}-hash2", name="Admin (break-glass)", method="admin",
                           ip="10.0.0.5", user_agent=None, expires_at="2099-01-01T00:00:00+00:00",
                           db=seeded.db)
    out, _ = seeded.build()
    members = _members(out)
    _assert_no_marker(members)
    copy = tmp_path / "copy.db"
    copy.write_bytes(members["database/gc.db"])
    conn = sqlite3.connect(copy)
    try:
        rows = conn.execute("SELECT name, method, ip, token_hash, created_at IS NOT NULL "
                            "FROM web_sessions ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [("Ryan C", "card", "203.0.113.9", None, 1),
                    ("Admin (break-glass)", "admin", "10.0.0.5", None, 1)]


def test_labcore_and_lem_urls_are_reported_by_name(seeded, monkeypatch):
    monkeypatch.setenv("LABCORE_URL", "https://labcore.example/x")
    monkeypatch.delenv("LEM_URL", raising=False)
    out, _ = seeded.build()
    env = json.loads(_members(out)["environment.json"])["env"]
    assert env["LABCORE_URL"] == "set" and env["LEM_URL"] == "unset"
    assert b"labcore.example" not in _members(out)["environment.json"]
