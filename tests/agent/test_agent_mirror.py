import csv
import hashlib
import io
import json
import os

import pytest

from fakehub import CSV_HEADER as HUB_HEADER
from gc_agent import mirror
from gc_agent.client import HubClient
from gc_agent.ledger import Ledger
from gc_agent.mirror import CSV_HEADER, Mirror, MirrorError


def _line(*vals):
    b = io.StringIO()
    csv.writer(b).writerow(vals)
    return b.getvalue()


HEADER_LINE = _line(*CSV_HEADER)


def _rows(*seqs):
    return [{"seq": s, "line": _line("L%d" % s, "2026-09-28 10:00:0%d" % (s % 10), 'q"uote,é', s)}
            for s in seqs]


def _sidecar(p):
    return json.loads((p.parent / (p.name + ".gchub.json")).read_text(encoding="utf-8"))


@pytest.fixture
def m(tmp_path):
    lg = Ledger(tmp_path / "ledger.db")
    return lg, Mirror(str(tmp_path / "out" / "results.csv"), lg)


def test_header_literal_matches_hub_contract():
    assert CSV_HEADER == HUB_HEADER and len(CSV_HEADER) == 31


def test_hub_header_literal_matches_distill():
    # distill.py needs numpy; compare against its source text instead of importing it.
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "distill.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "CSV_HEADER":
            assert ast.literal_eval(node.value) == CSV_HEADER
            return
    pytest.fail("CSV_HEADER not found in distill.py")


def test_creates_file_with_header_and_verbatim_rows(m):
    lg, mr = m
    rows = _rows(1, 2)
    assert mr.append(CSV_HEADER, rows) == 2
    p = mr.path_obj
    assert p.read_bytes() == (HEADER_LINE + rows[0]["line"] + rows[1]["line"]).encode("utf-8")
    sc = _sidecar(p)
    assert sc["size"] == p.stat().st_size and sc["seq"] == 2
    assert sc["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert "adopted_at" in sc and "pending" not in sc
    assert lg.results_seq() == 2


def test_rows_at_or_below_seq_are_skipped(m):
    lg, mr = m
    mr.append(CSV_HEADER, _rows(1, 2))
    before = mr.path_obj.read_bytes()
    assert mr.append(CSV_HEADER, _rows(2, 3)) == 1
    assert mr.path_obj.read_bytes() == before + _rows(3)[0]["line"].encode("utf-8")
    assert lg.results_seq() == 3


def test_existing_file_without_sidecar_is_refused(m):
    lg, mr = m
    mr.path_obj.parent.mkdir(parents=True)
    mr.path_obj.write_bytes(HEADER_LINE.encode())
    with pytest.raises(MirrorError, match="adopt"):
        mr.append(CSV_HEADER, _rows(1))
    assert mr.path_obj.read_bytes() == HEADER_LINE.encode()
    assert lg.results_seq() == 0


@pytest.mark.parametrize("tamper,word", [
    (lambda b: b[:-3] + b"XYZ", "changed"),
    (lambda b: b + b"extra\r\n", "changed"),
    (lambda b: b[:10], "shrunk"),
])
def test_changed_or_shrunk_file_is_refused(m, tamper, word):
    lg, mr = m
    mr.append(CSV_HEADER, _rows(1))
    tampered = tamper(mr.path_obj.read_bytes())
    mr.path_obj.write_bytes(tampered)
    with pytest.raises(MirrorError, match=word):
        mr.append(CSV_HEADER, _rows(2))
    assert mr.path_obj.read_bytes() == tampered


def test_missing_file_with_sidecar_is_refused(m):
    lg, mr = m
    mr.append(CSV_HEADER, _rows(1))
    os.unlink(str(mr.path_obj))
    with pytest.raises(MirrorError, match="missing"):
        mr.append(CSV_HEADER, _rows(2))
    assert not mr.path_obj.exists()


def test_other_header_is_refused(m):
    lg, mr = m
    with pytest.raises(MirrorError, match="header"):
        mr.append(CSV_HEADER[:-1], _rows(1))
    assert not mr.path_obj.exists()


def test_unterminated_line_is_refused(m):
    lg, mr = m
    with pytest.raises(MirrorError, match="terminat"):
        mr.append(CSV_HEADER, [{"seq": 1, "line": "a,b"}])


def test_pending_rolls_forward_after_crash(m):
    lg, mr = m
    mr.append(CSV_HEADER, _rows(1))
    # Simulate a crash after the append but before the sidecar commit.
    extra = _rows(2)[0]["line"].encode()
    data = mr.path_obj.read_bytes()
    sc = _sidecar(mr.path_obj)
    sc["pending"] = {"size": len(data + extra), "sha256": hashlib.sha256(data + extra).hexdigest(),
                     "seq": 2}
    (mr.path_obj.parent / "results.csv.gchub.json").write_text(json.dumps(sc))
    mr.path_obj.write_bytes(data + extra)
    assert mr.append(CSV_HEADER, _rows(2, 3)) == 1      # 2 is already there
    assert mr.path_obj.read_bytes() == data + extra + _rows(3)[0]["line"].encode()
    assert _sidecar(mr.path_obj)["seq"] == 3


def test_adopt_checks_header_and_writes_sidecar(tmp_path):
    lg = Ledger(tmp_path / "l.db")
    lg.set_results_seq(7)
    p = tmp_path / "old.csv"
    p.write_bytes((HEADER_LINE + _line("A1", "x") + _line("A2", "y")).encode())
    info = mirror.adopt(str(p), lg.results_seq())
    assert info["size"] == p.stat().st_size
    assert info["last_row"].startswith("A2,y")
    sc = _sidecar(p)
    assert sc["seq"] == 7 and sc["size"] == p.stat().st_size
    mr = Mirror(str(p), lg)
    assert mr.append(CSV_HEADER, _rows(8)) == 1


def test_adopt_refuses_wrong_header(tmp_path):
    p = tmp_path / "old.csv"
    p.write_bytes(_line("Lab ID", "Other").encode())
    with pytest.raises(MirrorError, match="header"):
        mirror.adopt(str(p), 0)
    assert not (tmp_path / "old.csv.gchub.json").exists()


def test_inspect_reports_without_writing(tmp_path):
    p = tmp_path / "old.csv"
    p.write_bytes((HEADER_LINE + _line("A1", "x")).encode())
    info = mirror.inspect(str(p))
    assert info == {"exists": True, "size": p.stat().st_size, "header_ok": True,
                    "last_row": _line("A1", "x").rstrip("\r\n"), "adopted": False}
    assert not (tmp_path / "old.csv.gchub.json").exists()
    assert mirror.inspect(str(tmp_path / "none.csv"))["exists"] is False


def test_adopt_after_refusal_lets_append_proceed(m):
    lg, mr = m
    mr.append(CSV_HEADER, _rows(1))
    with open(str(mr.path_obj), "ab") as fh:
        fh.write(b"hand edit\r\n")
    with pytest.raises(MirrorError):
        mr.append(CSV_HEADER, _rows(2))
    mirror.adopt(str(mr.path_obj), lg.results_seq())
    assert mr.append(CSV_HEADER, _rows(2)) == 1


def test_adopt_missing_file_clears_sidecar(m):
    lg, mr = m
    mr.append(CSV_HEADER, _rows(1))
    os.unlink(str(mr.path_obj))
    assert mirror.adopt(str(mr.path_obj), lg.results_seq())["exists"] is False
    assert mr.append(CSV_HEADER, _rows(2)) == 1
    assert mr.path_obj.read_bytes().startswith(HEADER_LINE.encode())


def test_sync_pages_through_hub(tmp_path, hub):
    lg = Ledger(tmp_path / "l.db")
    hub.rows = _rows(*range(1, 8))
    mr = Mirror(str(tmp_path / "r.csv"), lg, page_size=3)
    n = mr.sync(HubClient(hub.url, hub.token, timeout=5))
    assert n == 7 and lg.results_seq() == 7
    afters = [r.path for r in hub.by_path("/api/agent/results")]
    assert afters == ["/api/agent/results?after=0&limit=3", "/api/agent/results?after=3&limit=3",
                      "/api/agent/results?after=6&limit=3"]
    assert mr.sync(HubClient(hub.url, hub.token, timeout=5)) == 0


def test_sync_http_error_raises_and_keeps_seq(tmp_path, hub):
    lg = Ledger(tmp_path / "l.db")
    hub.results_status = 503
    mr = Mirror(str(tmp_path / "r.csv"), lg)
    with pytest.raises(mirror.MirrorFetchError):
        mr.sync(HubClient(hub.url, hub.token, timeout=5))
    assert lg.results_seq() == 0
