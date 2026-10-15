"""The Learn page's tutorials: the committed manifest and its screenshots.

``static/learn/tutorials.json`` and ``static/learn/shots/*.webp`` are made by
``tools/learn_capture.py`` (run by hand; it boots its own hub and drives
Chrome). This checks what was committed, with no browser: the manifest's
shape, the five tutorials in order, every image a WebP of the manifest's
size (read from the RIFF header: no PIL), every highlight box inside the
picture, plain one-sentence captions, and no screenshot left unreferenced.
"""
from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LEARN = ROOT / "static" / "learn"
MANIFEST = LEARN / "tutorials.json"
SHOTS = LEARN / "shots"
IDS = ["search", "edit", "queue", "download", "upload"]


class UnknownWebP(Exception):
    """A RIFF/WEBP file whose first chunk this reader does not know."""


def webp_size(data: bytes) -> tuple[int, int]:
    """``(width, height)`` of a WebP from its header. Raises ``ValueError``
    when the bytes are not a WebP, ``UnknownWebP`` for a chunk it can't read."""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a RIFF/WEBP file")
    chunk = data[12:16]
    body = data[20:]
    if chunk == b"VP8 ":                      # lossy: frame tag, start code, 14-bit sizes
        if body[3:6] != b"\x9d\x01\x2a":
            raise ValueError("VP8 chunk without its start code")
        w, h = struct.unpack("<HH", body[6:10])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L":                      # lossless: 0x2f, then 14-bit sizes minus one
        if body[0] != 0x2F:
            raise ValueError("VP8L chunk without its signature")
        bits = int.from_bytes(body[1:5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":                      # extended: 24-bit canvas sizes minus one
        return int.from_bytes(body[4:7], "little") + 1, int.from_bytes(body[7:10], "little") + 1
    raise UnknownWebP(f"unknown first chunk {chunk!r}")


def test_webp_size_reads_each_header_kind():
    vp8 = b"RIFF" + b"\0" * 4 + b"WEBPVP8 " + b"\0" * 4 + b"\0\0\0\x9d\x01\x2a" + struct.pack("<HH", 1280, 800)
    vp8l_bits = (1280 - 1) | ((800 - 1) << 14)
    vp8l = b"RIFF" + b"\0" * 4 + b"WEBPVP8L" + b"\0" * 4 + b"\x2f" + vp8l_bits.to_bytes(4, "little") + b"\0" * 8
    vp8x = (b"RIFF" + b"\0" * 4 + b"WEBPVP8X" + b"\0" * 4 + b"\0" * 4
            + (1280 - 1).to_bytes(3, "little") + (800 - 1).to_bytes(3, "little") + b"\0" * 8)
    assert webp_size(vp8) == webp_size(vp8l) == webp_size(vp8x) == (1280, 800)
    with pytest.raises(ValueError):
        webp_size(b"\x89PNG\r\n\x1a\n" + b"\0" * 40)


def load_manifest():
    """The committed manifest (a missing one fails each test, not an error)."""
    if not MANIFEST.is_file():
        pytest.fail(f"{MANIFEST.relative_to(ROOT)} is missing: run tools/learn_capture.py")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _steps(manifest):
    for t in manifest["tutorials"]:
        for n, step in enumerate(t["steps"], start=1):
            yield t["id"], n, step


def test_the_manifest_has_the_five_tutorials_in_order():
    manifest = load_manifest()
    assert manifest["version"] == 1
    assert manifest["size"] == [1280, 800]
    ids = [t["id"] for t in manifest["tutorials"]]
    assert ids == IDS, ids
    assert len(set(ids)) == len(ids)
    for t in manifest["tutorials"]:
        assert set(t) == {"id", "title", "summary", "steps"}, (t["id"], sorted(t))
        assert isinstance(t["title"], str) and 0 < len(t["title"]) <= 40, (t["id"], t["title"])
        assert isinstance(t["summary"], str) and 0 < len(t["summary"]) <= 120, (t["id"], t["summary"])
        assert 3 <= len(t["steps"]) <= 7, (t["id"], len(t["steps"]))


def test_every_caption_is_one_plain_sentence():
    manifest = load_manifest()
    for tid, n, step in _steps(manifest):
        assert set(step) == {"image", "caption", "box"}, (tid, n, sorted(step))
        cap = step["caption"]
        assert isinstance(cap, str) and 10 <= len(cap) <= 150, (tid, n, cap)
        assert "<" not in cap, (tid, n, cap)
        assert cap.endswith("."), (tid, n, cap)
        assert cap == cap.strip() and "\n" not in cap, (tid, n, cap)


def test_every_box_is_inside_the_picture():
    manifest = load_manifest()
    for tid, n, step in _steps(manifest):
        box = step["box"]
        if box is None:
            continue
        assert isinstance(box, list) and len(box) == 4, (tid, n, box)
        assert all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   for v in box), (tid, n, box)
        x, y, w, h = box
        assert x >= 0 and y >= 0, (tid, n, box)
        assert w > 0.005 and h > 0.005, (tid, n, box)
        assert x + w <= 1 + 1e-9 and y + h <= 1 + 1e-9, (tid, n, box)


def test_every_image_is_a_webp_of_the_manifest_size():
    manifest = load_manifest()
    size = tuple(manifest["size"])
    unknown = []
    for tid, n, step in _steps(manifest):
        rel = step["image"]
        assert isinstance(rel, str) and rel.startswith("shots/") and rel.endswith(".webp"), (tid, n, rel)
        path = (LEARN / rel).resolve()
        assert LEARN.resolve() in path.parents, (tid, n, rel)
        assert path.is_file(), f"{tid} step {n}: {rel} is missing"
        try:
            got = webp_size(path.read_bytes()[:64])
        except UnknownWebP as exc:
            unknown.append(f"{rel}: {exc}")
            continue
        except ValueError as exc:
            pytest.fail(f"{tid} step {n}: {rel} is not a WebP ({exc})")
        assert got == size, (tid, n, rel, got)
    if unknown:
        pytest.skip("can't read the size of: " + "; ".join(unknown))


def test_every_image_is_used_once_and_no_shot_is_left_over():
    manifest = load_manifest()
    images = [step["image"] for _tid, _n, step in _steps(manifest)]
    assert len(images) == len(set(images)), "an image is used by two steps"
    on_disk = sorted("shots/" + p.name for p in SHOTS.iterdir() if p.is_file()) if SHOTS.is_dir() else []
    unreferenced = sorted(set(on_disk) - set(images))
    assert unreferenced == [], f"unreferenced files under static/learn/shots: {unreferenced}"
