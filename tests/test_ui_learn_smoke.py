"""v8.2: the Learn page in a real browser, signed in.

* light and dark at 1440x900, 1366x768 and a phone (390x844): every sketch
  drawn, no sideways scroll, WCAG AA on every visible text, the sidebar's
  Learn marked as the page;
* the walk-throughs: one card per tutorial in static/learn/tutorials.json;
  Watch opens the player on step 1 with its screenshot and caption, it
  steps by itself, Next / Back / a dot / Pause work, Escape closes it.
Skipped when selenium or a Chrome/chromedriver can't be started.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("selenium.webdriver")

from bootapp import booted, browser_sign_in  # noqa: E402
from test_ui_setup_pages_smoke import CONTRAST_JS, _driver, _js  # noqa: E402
from ui_wait import wait_for  # noqa: E402

SHOTS = os.environ.get("GC_UI_SHOTS")
MANIFEST = json.loads((ROOT / "static" / "learn" / "tutorials.json").read_text(encoding="utf-8")) \
    if (ROOT / "static" / "learn" / "tutorials.json").exists() else None


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("learn-ui")
    with booted(tmp) as (port, _proc, _data, _home):
        drv = _driver()
        try:
            browser_sign_in(drv, port)
            yield drv, f"http://127.0.0.1:{port}"
        finally:
            drv.quit()


def _open(drv, base, theme, size):
    drv.execute_cdp_cmd("Emulation.setDeviceMetricsOverride",
                        {"width": size[0], "height": size[1], "deviceScaleFactor": 1, "mobile": size[0] < 600})
    drv.get(base + "/static/favicon.svg")
    _js(drv, "localStorage.setItem('gc.theme', arguments[0]);", theme)
    drv.get(base + "/learn")
    assert wait_for(drv, lambda: _js(drv, "return document.querySelectorAll('.lc-fig').length > 0 && "
                                          "[...document.querySelectorAll('.lc-fig')].every(f => f.querySelector('svg path'))"))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(1440, 900), (1366, 768), (390, 844)], ids=lambda s: f"{s[0]}x{s[1]}")
def test_the_learn_page_in_both_themes_at_every_size(page, theme, size):
    drv, base = page
    _open(drv, base, theme, size)
    assert _js(drv, "return document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    for sec in ("learn-read", "learn-fuels", "learn-seen", "learn-howto"):
        assert _js(drv, f"return !!document.querySelector('[data-testid={sec}]')"), sec
    assert _js(drv, "return document.querySelectorAll('[data-testid=learn-fuel]').length") == 7
    assert _js(drv, "return document.querySelectorAll('[data-testid=learn-seen-card]').length") == 7
    if size[0] >= 1000:
        assert _js(drv, "return document.querySelector('[data-testid=nav-learn]').getAttribute('aria-current')") == "page"
    js = CONTRAST_JS
    bad = []
    for k, y in enumerate(range(0, _js(drv, "return document.documentElement.scrollHeight"), size[1])):
        _js(drv, "window.scrollTo(0, arguments[0]);", y)
        bad += _js(drv, js)
        if SHOTS and theme == "light" and size == (1440, 900):
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            drv.save_screenshot(str(Path(SHOTS) / f"learn-scroll-{k}.png"))
    assert bad == [], (theme, size, bad[:5])
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        _js(drv, "window.scrollTo(0, 0);")
        drv.save_screenshot(str(Path(SHOTS) / f"learn-{size[0]}x{size[1]}-{theme}.png"))


@pytest.mark.skipif(MANIFEST is None, reason="static/learn/tutorials.json not made yet")
def test_a_walk_through_plays_and_steps(page):
    drv, base = page
    _open(drv, base, "light", (1440, 900))
    ids = [t["id"] for t in MANIFEST["tutorials"]]
    assert wait_for(drv, lambda: _js(drv, "return document.querySelectorAll('[data-testid=learn-tutorial]').length") == len(ids))
    first = MANIFEST["tutorials"][0]
    n = len(first["steps"])
    _js(drv, f"document.querySelector('[data-testid=learn-watch-{first['id']}]').click();")
    assert wait_for(drv, lambda: _js(drv, "return document.querySelector('[data-testid=tut-player]').open"))
    count = "return document.querySelector('[data-testid=tut-count]').textContent"
    caption = "return document.querySelector('[data-testid=tut-caption]').textContent"
    assert _js(drv, count) == f"Step 1 of {n}"
    assert _js(drv, caption) == first["steps"][0]["caption"]
    assert wait_for(drv, lambda: _js(drv, "const i = document.querySelector('[data-testid=tut-img]');"
                                          "return i.complete && i.naturalWidth"))
    # it plays by itself
    assert wait_for(drv, lambda: _js(drv, count) == f"Step 2 of {n}", timeout=12)
    _js(drv, "document.querySelector('[data-testid=tut-play]').click();")          # pause
    assert _js(drv, "return document.querySelector('[data-testid=tut-play]').textContent") == "Play"
    _js(drv, "document.querySelector('[data-testid=tut-next]').click();")
    assert _js(drv, count) == f"Step 3 of {n}"
    _js(drv, "document.querySelector('[data-testid=tut-prev]').click();")
    assert _js(drv, count) == f"Step 2 of {n}"
    _js(drv, f"document.querySelectorAll('#tut-dots button')[{n - 1}].click();")
    assert _js(drv, count) == f"Step {n} of {n}"
    assert _js(drv, caption) == first["steps"][-1]["caption"]
    box = first["steps"][-1]["box"]
    assert _js(drv, "return document.getElementById('tut-box').hidden") is (box is None)
    assert _js(drv, "return document.querySelector('[data-testid=tut-play]').textContent") == "Watch again"
    if SHOTS:
        drv.save_screenshot(str(Path(SHOTS) / "learn-player-1440x900.png"))
    assert _js(drv, CONTRAST_JS) == []
    from selenium.webdriver.common.keys import Keys
    drv.find_element("css selector", "[data-testid=tut-player]").send_keys(Keys.ESCAPE)
    assert wait_for(drv, lambda: not _js(drv, "return document.querySelector('[data-testid=tut-player]').open"))


def test_help_sends_readers_to_learn(page):
    drv, base = page
    drv.get(base + "/help")
    assert wait_for(drv, lambda: _js(drv, "return !!document.querySelector('[data-testid=help-page] a[href=\"/learn\"]')"))
