"""The Learn page's tutorials: the committed manifest and its screenshots.

``static/learn/tutorials.json``, ``static/learn/shots/*.webp`` (light) and
``static/learn/shots/dark/*.webp`` (dark) are made by ``tools/learn_capture.py``
(run by hand; it boots its own hub and drives Chrome). This checks what was
committed, with no browser: the manifest's shape, the five tutorials in
order, every image (``image`` and ``image_dark``) a WebP of the manifest's
size (read from the RIFF header: no PIL), every highlight box (``box`` and
``box_dark``) inside the picture and the dark one on the same element as the
light one, plain one-sentence captions, and no screenshot left unreferenced
in either folder.
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
DARK = SHOTS / "dark"
IDS = ["search", "edit", "queue", "download", "upload"]
STEP_KEYS = {"image", "caption", "box", "image_dark", "box_dark"}
# each theme's picture and its highlight box
THEMES = {"light": ("image", "box"), "dark": ("image_dark", "box_dark")}
BOX_DRIFT = 0.01            # how far a dark box may sit from the light one (fraction of the picture)


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
        assert set(step) == STEP_KEYS, (tid, n, sorted(step))
        cap = step["caption"]
        assert isinstance(cap, str) and 10 <= len(cap) <= 150, (tid, n, cap)
        assert "<" not in cap, (tid, n, cap)
        assert cap.endswith("."), (tid, n, cap)
        assert cap == cap.strip() and "\n" not in cap, (tid, n, cap)


def test_every_box_is_inside_the_picture():
    manifest = load_manifest()
    for tid, n, step in _steps(manifest):
        for theme, (_image, key) in THEMES.items():
            assert key in step, f"{tid} step {n}: no {key}"
            box = step[key]
            if box is None:
                continue
            where = (tid, n, theme, box)
            assert isinstance(box, list) and len(box) == 4, where
            assert all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                       for v in box), where
            x, y, w, h = box
            assert x >= 0 and y >= 0, where
            assert w > 0.005 and h > 0.005, where
            assert x + w <= 1 + 1e-9 and y + h <= 1 + 1e-9, where


def test_the_dark_box_highlights_what_the_light_one_does():
    manifest = load_manifest()
    for tid, n, step in _steps(manifest):
        assert "box_dark" in step, f"{tid} step {n}: no box_dark"
        light, dark = step["box"], step["box_dark"]
        assert (light is None) == (dark is None), (tid, n, light, dark)
        if light is not None:
            drift = max(abs(a - b) for a, b in zip(light, dark))
            assert drift <= BOX_DRIFT, f"{tid} step {n}: box {light} vs box_dark {dark}"


@pytest.mark.parametrize("theme", list(THEMES))
def test_every_image_is_a_webp_of_the_manifest_size(theme):
    manifest = load_manifest()
    size = tuple(manifest["size"])
    key = THEMES[theme][0]
    folder = "shots/dark/" if theme == "dark" else "shots/"
    unknown = []
    for tid, n, step in _steps(manifest):
        assert key in step, f"{tid} step {n}: no {key}"
        rel = step[key]
        assert isinstance(rel, str) and rel.startswith(folder) and rel.endswith(".webp"), (tid, n, rel)
        assert "/" not in rel[len(folder):], (tid, n, rel)
        if theme == "dark":       # the light picture's name, in shots/dark/
            assert rel == "shots/dark/" + step["image"][len("shots/"):], (tid, n, rel, step["image"])
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
    images = [step.get(key) for _tid, _n, step in _steps(manifest) for key, _box in THEMES.values()]
    assert all(isinstance(i, str) for i in images), "a step has no image or no image_dark"
    assert len(images) == len(set(images)), "an image is used by two steps (or by both themes)"
    # everything under shots/ (light) and shots/dark/; any other folder counts as left over
    entries = [p for p in SHOTS.rglob("*") if p != DARK] if SHOTS.is_dir() else []
    on_disk = sorted(p.relative_to(LEARN).as_posix() + ("" if p.is_file() else "/") for p in entries)
    unreferenced = sorted(set(on_disk) - set(images))
    assert unreferenced == [], f"unreferenced files under static/learn/shots: {unreferenced}"
    assert any(p.startswith("shots/dark/") for p in on_disk), "no dark screenshots in static/learn/shots/dark"
