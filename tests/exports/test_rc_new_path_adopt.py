"""v2.0.0 RC: "New path, then Adopt" is the documented way to point an
instrument at the share CSV LEM already tails. Between the two steps the
file exists without a sidecar; the pass right after New path must not raise
an ERROR notification for that (it is an info: "adopt it next"). Any other
refusal, or the same file refused without a New path, is still an error."""
from __future__ import annotations

from pathlib import Path

import pytest

import exports
from exports_testlib import add_final, header_bytes, make_db, pending


@pytest.fixture()
def db(tmp_path) -> Path:
    return make_db(tmp_path)


@pytest.fixture()
def notes() -> list:
    return []


@pytest.fixture()
def exp(db, tmp_path, notes):
    return exports.HubExporter(db=db, data_dir=tmp_path / "data",
                               notifier=lambda level, msg: notes.append((level, msg)),
                               retry_sleep=lambda s: None)


def _v1_file(path: Path) -> bytes:
    body = header_bytes() + b"".join(
        f"v1-{i},2026-09-01 00:00:0{i}".encode() + b"," * 29 + b"\r\n" for i in range(3))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return body


def test_new_path_then_adopt_raises_no_error(exp, db, tmp_path, notes):
    share = tmp_path / "share" / "distill_results.csv"
    before = _v1_file(share)
    add_final(db)
    exp.new_path("gc1", share)
    exp.tick()                                   # the wake right after New path
    exp.tick()                                   # ...and the next interval, before Adopt
    assert [n for n in notes if n[0] == "error"] == [], notes
    infos = [m for level, m in notes if level == "info"]
    assert len(infos) == 1 and "adopt" in infos[0].lower(), notes
    assert exp.status("gc1")["refused"] == "no-sidecar"     # still shown on the admin page
    assert share.read_bytes() == before                      # nothing appended before Adopt
    exp.adopt("gc1")
    exp.tick()
    assert pending(db) == []
    assert share.read_bytes().startswith(before)
    assert [n for n in notes if n[0] == "error"] == [], notes


def test_a_no_sidecar_file_without_new_path_is_still_an_error(exp, db, notes):
    _v1_file(exp.export_path("gc1"))
    add_final(db)
    exp.tick()
    assert [level for level, _m in notes] == ["error"]


def test_after_adopt_a_later_no_sidecar_refusal_is_an_error_again(exp, db, tmp_path, notes):
    share = tmp_path / "share" / "gc1.csv"
    _v1_file(share)
    exp.new_path("gc1", share)
    exp.tick()
    exp.adopt("gc1")
    add_final(db)
    exp.tick()
    exports.sidecar_path(share).unlink()         # someone deletes the sidecar later
    add_final(db)
    exp.tick()
    assert [level for level, _m in notes if level == "error"] == ["error"], notes


def test_new_path_to_another_file_then_a_different_refusal_is_an_error(exp, db, tmp_path, notes):
    share = tmp_path / "share" / "gc1.csv"
    share.parent.mkdir(parents=True)
    share.write_bytes(b"not,the,header\r\n")
    exports.sidecar_path(share).write_text("{not json", encoding="utf-8")
    add_final(db)
    exp.new_path("gc1", share)
    exp.tick()
    assert [level for level, _m in notes] == ["error"], notes
