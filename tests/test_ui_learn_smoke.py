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
    assert _js(drv, "return document.querySelectorAll('[data-testid=learn-case]').length") == 7
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
    assert wait_for(drv, lambda: _js(drv, "return document.querySelectorAll('[data-testid=learn-tutorial]').length")
                    == len(ids) + 1)                                   # v8.3: the lesson first
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


def test_the_lesson_steps_through_its_parts_of_the_chart(page):
    """v8.3: Reading a chromatogram is a self-paced lesson: Next / Back / a
    dot, each step marking its part of the chart; the last step offers to
    start over; How to lists it first."""
    drv, base = page
    _open(drv, base, "light", (1440, 900))
    count = "return document.querySelector('[data-testid=lesson-count]').textContent"
    title = "return document.querySelector('[data-testid=lesson-title]').textContent"
    n = int(_js(drv, count).split(" of ")[1])
    assert _js(drv, count) == f"Step 1 of {n}" and 8 <= n <= 12
    assert _js(drv, title) == "Time runs left to right"
    assert _js(drv, "return document.querySelectorAll('#lesson-chart .lc-focus').length") > 0
    first = _js(drv, title)
    _js(drv, "document.querySelector('[data-testid=lesson-next]').click();")
    assert _js(drv, count) == f"Step 2 of {n}" and _js(drv, title) != first
    _js(drv, "document.querySelectorAll('#lesson-dots button')[3].click();")       # Find the hump
    assert _js(drv, title) == "Find the hump"
    assert _js(drv, "return [...document.querySelectorAll('#lesson-chart text.lc-band-on-label')].map(t => t.textContent)") == ["Hump"]
    _js(drv, "document.querySelectorAll('#lesson-dots button')[9].click();")       # the difference
    assert _js(drv, "return !!document.querySelector('#lesson-chart .lc-above') && !!document.querySelector('#lesson-chart .lc-below')")
    _js(drv, f"document.querySelectorAll('#lesson-dots button')[{n - 1}].click();")
    assert _js(drv, "return document.querySelector('[data-testid=lesson-next]').textContent") == "Start over"
    _js(drv, "document.querySelector('[data-testid=lesson-next]').click();")
    assert _js(drv, count) == f"Step 1 of {n}"
    assert wait_for(drv, lambda: _js(drv, "return document.querySelector('[data-testid=learn-tutorial] button').dataset.testid")
                    == "learn-watch-lesson")


def test_the_cases_share_one_chart_against_diesel_2(page):
    """v8.3: What we see most is one chart: Diesel #2 (grey) with every case
    over it; the picked case is bold with its band and its difference, and
    its notes are beside it; water is a flat curve."""
    drv, base = page
    _open(drv, base, "light", (1440, 900))
    names = _js(drv, "return [...document.querySelectorAll('[data-testid=learn-case]')].map(b => b.textContent)")
    assert names[0] == "Gasoline in diesel" and "Water: a flat curve" in names, names
    assert _js(drv, "return document.querySelector('[data-testid=learn-case]').getAttribute('aria-pressed')") == "true"
    assert _js(drv, "return document.querySelectorAll('#cases-chart path.lc-case').length") == 6
    assert _js(drv, "return document.querySelectorAll('#cases-chart path.lc-case-on').length") == 1
    detail = "return document.querySelector('[data-testid=case-detail]').textContent"
    assert "Lighter C5–C11" in _js(drv, detail)
    _js(drv, "[...document.querySelectorAll('[data-testid=learn-case]')].find(b => b.textContent.startsWith('Water')).click();")
    assert "check the water content" in _js(drv, detail)
    assert _js(drv, "return [...document.querySelectorAll('[data-testid=learn-case]')].filter(b => b.getAttribute('aria-pressed') === 'true').map(b => b.textContent)") == ["Water: a flat curve"]
    assert _js(drv, "return document.querySelectorAll('#cases-chart .lc-band-on').length") == 0   # water: the whole curve


@pytest.mark.skipif(MANIFEST is None or not any("image_dark" in st for t in MANIFEST["tutorials"] for st in t["steps"]),
                    reason="no dark screenshots yet")
def test_the_walk_throughs_follow_the_theme(page):
    """v8.3: in the dark theme the player shows the dark screenshots, and
    switching the theme switches them."""
    drv, base = page
    _open(drv, base, "dark", (1440, 900))
    first = MANIFEST["tutorials"][0]
    assert wait_for(drv, lambda: _js(drv, f"return !!document.querySelector('[data-testid=learn-watch-{first['id']}]')"))
    _js(drv, f"document.querySelector('[data-testid=learn-watch-{first['id']}]').click();")
    assert wait_for(drv, lambda: _js(drv, "return document.querySelector('[data-testid=tut-player]').open"))
    _js(drv, "document.querySelector('[data-testid=tut-play]').click();")          # pause on step 1
    src = "return document.querySelector('[data-testid=tut-img]').getAttribute('src')"
    assert _js(drv, src).endswith("/" + first["steps"][0]["image_dark"]), _js(drv, src)
    assert wait_for(drv, lambda: _js(drv, "const i = document.querySelector('[data-testid=tut-img]'); return i.complete && i.naturalWidth"))
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        drv.save_screenshot(str(Path(SHOTS) / "learn-player-dark.png"))
    assert _js(drv, CONTRAST_JS) == []
    _js(drv, "document.documentElement.setAttribute('data-theme', 'light');")
    assert wait_for(drv, lambda: _js(drv, src).endswith("/" + first["steps"][0]["image"]))
