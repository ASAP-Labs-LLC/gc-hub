#!/usr/bin/env python3
"""Make the Learn page's tutorial screenshots and their manifest.

Run by hand from the repo root (Chrome, chromedriver, Selenium, numpy,
netCDF4 and ImageMagick's ``magick`` needed; nothing real is contacted):

    python tools/learn_capture.py                     # all five, into static/learn
    python tools/learn_capture.py --out /tmp/learn    # somewhere else
    python tools/learn_capture.py --only edit,queue   # re-take some; the rest of the manifest is kept
    python tools/learn_capture.py --keep-png /tmp/png # also keep Chrome's PNGs, to look at

What it does:

* builds a throwaway hub in a temp folder with real samples
  (``tests/ui_compare_harness.build``: ``tests/hub_boot.build_hub`` plus
  40329, a diesel with 5% gasoline, and two diesel standards, best fit on),
  and boots app.py on it the way the UI tests do (``tests/bootapp.booted``),
  through ``tests/qbench_stub_boot.py`` so a QBench upload never leaves the
  machine (each lab ID's outcome comes from a plan file; the API key, the
  saved sign-in and the token URL are fakes in the temp folder);
* signs in (``bootapp.sign_in`` + ``browser_sign_in``) and drives headless
  Chrome at exactly 1280x800, deviceScaleFactor 1, light theme, scrollbars
  hidden, animations, transitions and the text caret off, the build badge
  hidden; downloads go to the temp folder;
* for each step waits for the page to settle (data loaded, charts drawn,
  layout unchanged across several polls; no fixed sleeps), clears any toast,
  takes Chrome's PNG and converts it with ``magick in.png -quality 80
  out.webp`` (a lower quality only if a shot comes out over 120 KB);
* writes ``<out>/shots/<tutorial>-<n>.webp`` and ``<out>/tutorials.json``:

      {"version": 1, "size": [1280, 800],
       "tutorials": [{"id", "title", "summary",
                      "steps": [{"image": "shots/search-1.webp", "caption": "...",
                                 "box": [x, y, w, h] | null}]}]}

  where ``box`` is the highlighted element's rect (getBoundingClientRect
  over the viewport, padded by 6 px, clamped to the picture) as fractions.

Nothing under ``--out`` changes unless every step worked; then the old shots
of the tutorials it took are replaced (every shot, on a full run, so none is
left unreferenced) and the manifest is rewritten (``--only`` keeps the other
tutorials' entries). It prints each shot's size and box as it goes, and any
browser console errors. ``tests/test_learn_tutorials.py`` checks the result
in CI.
Never imports app.py: the app runs in its own process.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
for _p in (ROOT, TESTS, TESTS / "golden", TESTS / "fixtures"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

W, H = 1280, 800
PAD = 6                          # px around a highlighted element
MAX_BYTES = 120 * 1024           # per shot
QUALITIES = (80, 74, 68, 60)     # the first is the rule; the rest only when a shot is too big

TUTORIALS = [
    ("search", "Find a sample", "Filter the Samples list by lab ID or status and open a run."),
    ("edit", "Check a sample in Compare",
     "Compare a run with a standard, adjust the graphs, mark a region and edit the conclusion."),
    ("queue", "Queue reports", "Collect reports in the report queue, reopen one in Compare, or remove it."),
    ("download", "Download a report", "Download one report as a PDF, the whole queue as one ZIP, or the raw CDF."),
    ("upload", "Upload to QBench", "Send the queued reports to QBench and follow each one until it is sent."),
]

# The QBench stub's answer per lab ID (tests/qbench_stub_boot.py): the first
# report goes straight through; the next two take 3 s each, long enough to
# show the progress with Skip and Stop.
UPLOAD_PLAN = {"40329": "ok", "40304": "slow", "40298": "slow"}

# Steady pictures: no animation or transition mid-way, no blinking caret,
# no build badge ("dev" in a checkout).
CAPTURE_CSS = """
*, *::before, *::after { animation: none !important; transition: none !important;
  caret-color: transparent !important; scroll-behavior: auto !important; }
#app-version { visibility: hidden !important; }
"""
INJECT_JS = """
(() => {
  const css = %s;
  const add = () => {
    if (document.getElementById('learn-capture-style')) return;
    const s = document.createElement('style');
    s.id = 'learn-capture-style';
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  };
  if (document.documentElement) add();
  document.addEventListener('DOMContentLoaded', add);
})();
""" % json.dumps(CAPTURE_CSS)

# Everything that must be still before a shot: the scroll positions, the
# charts' drawn sizes, open dialogs and the drawer, and how much text there is.
SIGNATURE_JS = """
const r = (el) => { const b = el.getBoundingClientRect();
  return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; };
const vis = (el) => { const b = el.getBoundingClientRect(); return b.width > 0 && b.height > 0; };
const out = [document.readyState, document.fonts ? document.fonts.status : 'loaded',
             document.body ? document.body.innerText.length : 0];
for (const el of document.querySelectorAll('.js-plotly-plot')) {
  if (!vis(el)) continue;
  const fl = el._fullLayout;
  out.push(['plot', r(el), fl ? [fl.width, fl.height] : null, !!el.querySelector('.main-svg')]);
}
for (const el of document.querySelectorAll('dialog[open], .cmp-drawer:not([hidden]), .menu:not([hidden])'))
  if (vis(el)) out.push(['box', r(el)]);
for (const el of document.querySelectorAll('#detail, .rq-body, .cmp-drawer-body, #rows'))
  out.push(['scroll', el.scrollTop]);
out.push(['busy', document.querySelectorAll('.cmp.is-loading, [aria-busy="true"]').length]);
return JSON.stringify(out);
"""

# The union of the targets' rects, in viewport pixels (null: none visible).
RECT_JS = """
const els = arguments[0].flatMap(css => [...document.querySelectorAll(css)].slice(0, 1));
let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, missing = [];
arguments[0].forEach((css, i) => { if (!document.querySelector(css)) missing.push(css); });
for (const el of els) {
  const b = el.getBoundingClientRect();
  if (!b.width || !b.height) { missing.push('(not shown) ' + (el.dataset.testid || el.id || el.className)); continue; }
  x0 = Math.min(x0, b.left); y0 = Math.min(y0, b.top); x1 = Math.max(x1, b.right); y1 = Math.max(y1, b.bottom);
}
return {missing, rect: x1 > x0 ? [x0, y0, x1, y1] : null, vw: innerWidth, vh: innerHeight};
"""


class CaptureError(RuntimeError):
    pass


def tid(name: str) -> str:
    """The CSS selector of a ``data-testid``."""
    return f"[data-testid={name}]"


# ── the browser ──────────────────────────────────────────────────────────────

def make_driver(download_dir: Path):
    """Headless Chrome as the UI tests start it (``tests/test_ui_setup_pages_smoke._driver``),
    plus hidden scrollbars, at exactly 1280x800 and deviceScaleFactor 1, light."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--force-device-scale-factor=1", "--hide-scrollbars", f"--window-size={W},{H}",
                "--lang=en-US"):
        opts.add_argument(arg)
    opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    opts.add_experimental_option("prefs", {"download.default_directory": str(download_dir),
                                           "download.prompt_for_download": False})
    drv = webdriver.Chrome(options=opts)
    drv.execute_cdp_cmd("Emulation.setDeviceMetricsOverride",
                        {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False})
    drv.execute_cdp_cmd("Emulation.setEmulatedMedia",
                        {"features": [{"name": "prefers-color-scheme", "value": "light"},
                                      {"name": "prefers-reduced-motion", "value": "reduce"}]})
    drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": INJECT_JS})
    for cmd in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
        try:
            drv.execute_cdp_cmd(cmd, {"behavior": "allow", "downloadPath": str(download_dir)})
            break
        except Exception:  # noqa: BLE001 - an older Chrome has only one of them
            continue
    drv.set_script_timeout(120)
    return drv


class Capture:
    """Drives one signed-in browser through the tutorials and records the steps."""

    def __init__(self, drv, base: str, shots: Path, keep_png: Path | None):
        self.drv = drv
        self.base = base
        self.shots = shots
        self.keep_png = keep_png
        self.steps: dict[str, list] = {}

    # ── plumbing ──
    def js(self, script, *args):
        return self.drv.execute_script(script, *args)

    def wait(self, pred, what: str, timeout: float = 30.0, every: float = 0.1, soft: bool = False):
        """Poll ``pred`` until truthy; return its value. A script error counts as
        "not yet" (the page is still rendering). ``soft``: on a timeout, say so
        and go on (for a nicety, never for what a step shows)."""
        deadline = time.time() + timeout
        last = None
        while True:
            try:
                last = pred()
            except Exception as exc:  # noqa: BLE001 - DOM not ready yet
                last = None
                err = exc
            else:
                err = None
            if last:
                return last
            if time.time() > deadline:
                if soft:
                    print(f"  (gave up waiting for {what}; going on)")
                    return None
                raise CaptureError(f"timed out waiting for {what}" + (f" ({err})" if err else ""))
            time.sleep(every)

    def exists(self, css) -> bool:
        return bool(self.js("const e = document.querySelector(arguments[0]);"
                            "if (!e) return false; const b = e.getBoundingClientRect();"
                            "return b.width > 0 && b.height > 0;", css))

    def click(self, css, what=None):
        """Click like the operator would, without moving the mouse (JS click),
        once the element is shown and enabled."""
        self.wait(lambda: self.js("const e = document.querySelector(arguments[0]);"
                                  "if (!e || e.disabled) return false; const b = e.getBoundingClientRect();"
                                  "return b.width > 0 && b.height > 0;", css), what or f"{css} to be clickable")
        self.js("document.querySelector(arguments[0]).click();", css)

    def text(self, css) -> str:
        return self.js("const e = document.querySelector(arguments[0]); return e ? e.textContent : '';", css) or ""

    def reveal(self, css, block="center"):
        """Scroll ``css`` into view (instantly) in whatever scrolls it."""
        self.js("document.querySelector(arguments[0]).scrollIntoView({block: arguments[1], inline: 'nearest'});",
                css, block)

    def type_into(self, css, text):
        el = self.drv.find_element("css selector", css)
        el.send_keys(text)

    def park_mouse(self):
        """The pointer in an empty strip of the top bar: no hover state in the shot."""
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        ab = ActionBuilder(self.drv)
        ab.pointer_action.move_to_location(W // 2, 8)
        ab.perform()

    def reset_storage(self):
        """A fresh tab's storage (no queue, no remembered picks), light theme."""
        self.drv.get(self.base + "/static/favicon.svg")
        self.js("localStorage.clear(); sessionStorage.clear(); localStorage.setItem('gc.theme', 'light');")

    def settle(self, timeout=30.0):
        """Wait until nothing moves: the page loaded, fonts in, charts drawn,
        and the layout signature the same for 4 polls in a row (~0.4 s)."""
        self.js(INJECT_JS)
        self.wait(lambda: self.js("return document.readyState === 'complete' && "
                                  "(!document.fonts || document.fonts.status === 'loaded');"), "the page to load")
        self.wait(lambda: self.js(
            "return [...document.querySelectorAll('.js-plotly-plot')].every(p => {"
            " const b = p.getBoundingClientRect(); if (!b.width || !b.height) return true;"
            " return !!(p._fullLayout && p.querySelector('.main-svg')); });"), "the charts to draw")
        same, last = 0, None
        deadline = time.time() + timeout
        while same < 4:
            sig = self.js(SIGNATURE_JS)
            same = same + 1 if sig == last else 0
            last = sig
            if time.time() > deadline:
                raise CaptureError("the page never settled")
            time.sleep(0.1)
        self.js("return new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));")

    def unhover(self):
        """No chart hover label or spike line left over from the pointer."""
        self.js("document.querySelectorAll('.js-plotly-plot').forEach(p => {"
                " try { window.Plotly && Plotly.Fx.unhover(p); } catch (e) {} });")

    def clear_toast(self):
        self.js("const t = document.getElementById('toast'); if (t) { t.textContent = ''; t.className = 'toast'; }")

    def box(self, targets):
        """The targets' union rect as [x, y, w, h] fractions, padded and clamped."""
        if targets is None:
            return None
        targets = [targets] if isinstance(targets, str) else list(targets)
        got = self.js(RECT_JS, targets)
        if got["missing"] or not got["rect"]:
            raise CaptureError(f"no box: {got['missing'] or targets} not on screen")
        vw, vh = got["vw"], got["vh"]
        if (vw, vh) != (W, H):
            raise CaptureError(f"the viewport is {vw}x{vh}, not {W}x{H}")
        x0, y0, x1, y1 = got["rect"]
        x0, y0 = max(0.0, x0 - PAD), max(0.0, y0 - PAD)
        x1, y1 = min(float(W), x1 + PAD), min(float(H), y1 + PAD)
        if x1 - x0 < 8 or y1 - y0 < 8:
            raise CaptureError(f"{targets} is (almost) off screen: {got['rect']}")
        return [round(x0 / W, 4), round(y0 / H, 4), round((x1 - x0) / W, 4), round((y1 - y0) / H, 4)]

    def shot(self, tutorial: str, caption: str, target=None, *, keep_focus=False, toast=False, check=None):
        """One step: settle, tidy, box, PNG → WebP, recorded for the manifest.
        ``check`` (a callable) must still hold after the picture is taken."""
        if not keep_focus:
            self.js("const a = document.activeElement; if (a && a !== document.body && a.blur) a.blur();")
        if not toast:
            self.clear_toast()
        self.park_mouse()
        self.unhover()
        self.settle()
        if not toast:
            self.clear_toast()
        box = self.box(target)
        png = self.drv.get_screenshot_as_png()
        if check is not None and not check():
            raise CaptureError(f"{tutorial}: the page moved on while the shot was taken ({caption!r})")
        n = len(self.steps.setdefault(tutorial, [])) + 1
        name = f"{tutorial}-{n}"
        image = to_webp(png, self.shots / f"{name}.webp", self.keep_png and self.keep_png / f"{name}.png")
        self.steps[tutorial].append({"image": f"shots/{image.name}", "caption": caption, "box": box})
        size = image.stat().st_size
        print(f"  {name}.webp  {size / 1024:5.1f} KB  box={box}  {caption}")

    # ── page helpers ──
    def open_samples(self, path):
        self.drv.get(self.base + path)
        self.wait(lambda: self.js("return !!(window.GCSamples && GCSamples.ready)"), "the Samples page")
        self.wait(lambda: self.js("return document.querySelectorAll('[data-testid=sample-row]').length > 0"),
                  "the sample list")

    def overview_ready(self, lab):
        self.wait(lambda: self.text(tid("detail-lab")) == lab, f"run {lab} to open")
        self.wait(lambda: self.exists(tid("results-table")), "the results")
        self.wait(lambda: self.js("const c = document.getElementById('chrom');"
                                  "return !!(c && c._fullLayout && c.querySelector('.main-svg'));"), "the chromatogram")

    def compare_ready(self, standard=None):
        def ok():
            st = self.js("const c = window.GCSamples && GCSamples.state.compare;"
                         "return c && c.standard ? c.standard() : null;")
            return st and (standard is None or st == standard)
        self.wait(ok, f"Compare on {standard or 'a standard'}", 60)
        self.wait(lambda: self.js("return document.querySelectorAll('[data-testid=compare-finding]').length > 0"),
                  "the findings", 60)
        self.wait(lambda: self.js("const c = document.querySelector('.cmp');"
                                  "return c && !c.classList.contains('is-loading');"), "the analysis", 60)
        self.wait(lambda: self.js("return ['compare-trend', 'compare-diff'].every(t => {"
                                  " const p = document.querySelector('[data-testid=' + t + ']');"
                                  " return p && p._fullLayout && p.querySelector('.main-svg'); });"),
                  "the trend and difference charts", 60)

    def seed_queue(self, samples):
        """Reports in the queue as a bulk add makes them: each sample with its
        best-fit standard and the saved settings (no toast)."""
        self.wait(lambda: self.js("const S = GCSamples.state; return S.standards.length > 0 && !!S.settings;"),
                  "the standards and settings")
        self.js("""
            const S = GCSamples.state;
            // the list row, its best fit as the name Compare would open on (the row
            // carries {label, score}; pickStandard takes a name)
            const row = (s) => {
                const f = S.files.find(f => f.sample_id === s[0]) || {sample_id: s[0], lab_id: s[1]};
                const bf = f.best_fit && typeof f.best_fit === 'object' ? f.best_fit.label : f.best_fit;
                return Object.assign({}, f, {best_fit: bf || null});
            };
            GCReportQueue.addMany(arguments[0].map(s => GCCompare.reportItem({
                sample: row(s), standards: S.standards, settings: S.settings})), {quiet: true});""",
                [[sid, lab] for sid, lab in samples])
        self.wait(lambda: self.js("return GCReportQueue.count()") >= len(samples), "the seeded queue")

    def open_queue(self):
        self.click("#report-queue-btn", "the Report queue button")
        self.wait(lambda: self.js("const d = document.getElementById('report-queue-sheet'); return !!(d && d.open);"),
                  "the report queue sheet")

    def close_dialogs(self):
        self.js("document.querySelectorAll('dialog[open]').forEach(d => d.close());")


# ── conversion ───────────────────────────────────────────────────────────────

def magick() -> str:
    exe = shutil.which("magick") or "/opt/homebrew/bin/magick"
    if not Path(exe).exists() and not shutil.which(exe):
        raise CaptureError("ImageMagick's magick is needed to write WebP")
    return exe


def png_size(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise CaptureError("Chrome's screenshot is not a PNG")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def to_webp(png: bytes, out: Path, keep_png: Path | None) -> Path:
    if png_size(png) != (W, H):
        raise CaptureError(f"the screenshot is {png_size(png)}, not {(W, H)}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "shot.png"
        src.write_bytes(png)
        if keep_png:
            keep_png.parent.mkdir(parents=True, exist_ok=True)
            keep_png.write_bytes(png)
        for q in QUALITIES:
            subprocess.run([magick(), str(src), "-quality", str(q), str(out)], check=True)
            if out.stat().st_size <= MAX_BYTES:
                if q != QUALITIES[0]:
                    print(f"  ({out.name}: quality {q} to stay under {MAX_BYTES // 1024} KB)")
                return out
    raise CaptureError(f"{out.name} is {out.stat().st_size} bytes even at quality {QUALITIES[-1]}")


# ── the tutorials ────────────────────────────────────────────────────────────

def tut_search(c: Capture, ids):
    t = "search"
    c.reset_storage()
    c.open_samples("/samples")
    n_all = len(ids)
    c.wait(lambda: c.js("return document.querySelectorAll('[data-testid=sample-row]').length") == n_all,
           "every sample in the list")
    c.shot(t, "Click Samples in the sidebar: every run is listed, newest first, under the day it ran.",
           ".nav-item[data-nav=samples]")

    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.keys import Keys
    c.js("document.getElementById('rows').focus();")
    ActionChains(c.drv).key_down(Keys.CONTROL).send_keys("k").key_up(Keys.CONTROL).perform()
    c.wait(lambda: c.js("return document.activeElement && document.activeElement.id") == "filter-q",
           "Ctrl K to focus the filter")
    c.type_into(tid("filter-q"), "403")
    want = {ids["diesel"], ids["final"], ids["rerun"]}
    c.wait(lambda: set(c.js("return [...document.querySelectorAll('[data-testid=sample-row]')]"
                            ".map(r => Number(r.dataset.sampleId));")) == want, "the list to narrow to 403…")
    c.wait(lambda: "q=403" in c.js("return location.search"), "the filter in the address")
    c.shot(t, "Press Ctrl K, or click Filter lab ID, and type part of a lab ID: the list narrows as you type.",
           ".s-search", keep_focus=True)

    row = f"[data-testid=sample-row][data-sample-id='{ids['diesel']}']"
    c.shot(t, "Click a run to open it.", row)
    c.click(row)
    c.overview_ready("40329")
    c.shot(t, "Check the header: the lab ID, its status, the instrument, when it was injected and the method.",
           tid("detail-header"))
    c.shot(t, "Read the chromatogram on Overview; scroll down for the results and the distillation curve.",
           "#view-overview .chart-card")

    # the chips: clear the lab ID filter, then Not sent to QBench
    c.js("const q = document.getElementById('filter-q'); q.value = '';"
         "q.dispatchEvent(new Event('input', {bubbles: true}));")
    c.wait(lambda: "q=" not in c.js("return location.search"), "the filter to clear")
    c.click(tid("chip-notsent"))
    c.wait(lambda: c.js("return document.querySelector('[data-testid=chip-notsent]')"
                        ".getAttribute('aria-pressed')") == "true", "Not sent to QBench to be on")
    c.wait(lambda: "notsent" in c.js("return location.search"), "the chip in the address")
    c.wait(lambda: c.js("const n = GCSamples.state.files.length; return n > 0 && "
                        "document.querySelectorAll('[data-testid=sample-row]').length === n;"), "the filtered list")
    c.shot(t, "Use the instrument and status chips, or Not sent to QBench, to list only the runs you need.",
           [tid("instrument-chips"), tid("status-chips")])


def tut_edit(c: Capture, ids):
    t = "edit"
    sid = ids["diesel"]
    c.reset_storage()
    c.open_samples(f"/samples/{sid}")
    c.overview_ready("40329")
    c.click(tid("view-compare"))
    c.compare_ready()
    c.shot(t, "Open the Compare tab to check the run against a comparison standard.", tid("view-compare"))

    c.shot(t, "Check Compared with: it starts on the best-fit standard; pick another from the list to switch.",
           [".cmp-picker", tid("compare-bestfit")])

    c.click(tid("compare-adjust-toggle"))
    c.wait(lambda: c.exists(tid("compare-drawer")), "the Adjust drawer")
    c.settle()
    # thresholds, ranges and "Show up to (min)" in view (the drawer scrolls)
    c.js("const b = document.querySelector('.cmp-drawer-body'); b.scrollTop = b.scrollHeight;")
    c.shot(t, "Click Adjust to change the thresholds, the shaded ranges and Show up to (min); the graphs redraw.",
           tid("compare-drawer"))
    c.click(tid("adjust-close"))
    c.wait(lambda: not c.exists(tid("compare-drawer")), "the drawer to close")

    # Annotate: a real drag across the trend, over the gasoline's light end
    c.click(tid("compare-annotate"))
    c.wait(lambda: c.js("return document.querySelector('[data-testid=compare-trend]')._fullLayout.dragmode")
           == "select", "Annotate to arm the drag")
    c.reveal(".cmp-plot-trend")
    c.settle()
    drag_trend(c, 0.42, 1.02)
    c.wait(lambda: c.exists(tid("compare-annotation-pop")), "the region's note")
    c.type_into(tid("compare-annotation-text"), "Gasoline light ends")
    c.reveal(tid("compare-annotation-pop"))
    c.shot(t, "Click Annotate, drag across the trend chart to mark a region, type a note and click Save.",
           [tid("compare-annotation-pop"), tid("compare-annotate")])
    c.click(tid("compare-annotation-save"))
    c.wait(lambda: not c.exists(tid("compare-annotation-pop")), "the note to save")
    c.wait(lambda: c.js("return (document.querySelector('[data-testid=compare-trend]').layout.annotations || [])"
                        ".some(a => String(a.text).indexOf('Gasoline light ends') >= 0)"), "the marked region")

    # the conclusion: Add preset opens the editor with the presets under it
    c.wait(lambda: c.js("const b = document.querySelector('[data-testid=compare-preset-menu]');"
                        "return b && !b.hidden && !b.disabled;"), "Add preset")
    c.click(tid("compare-preset-menu"))
    c.wait(lambda: c.exists(tid("compare-presets")), "the presets")
    preset = "Possible contamination; results should be interpreted with caution."
    c.js("const b = [...document.querySelectorAll('[data-testid=compare-preset]')]"
         ".find(x => x.textContent === arguments[0]); b.click();", preset)
    c.wait(lambda: preset in c.js("return document.querySelector('[data-testid=compare-conclusion-input]').value"),
           "the preset in the conclusion")
    c.reveal(tid("compare-conclusion"))
    c.shot(t, "Under Conclusion, click Edit to change the wording, or Add preset to put in a stock sentence.",
           tid("compare-conclusion"))
    c.click(tid("compare-conclusion-save"))
    c.wait(lambda: preset in c.text(tid("compare-conclusion-text")), "the saved conclusion")
    c.reveal(tid("compare-conclusion"))
    c.shot(t, "Click Save: this run's reports now print your conclusion instead of the generated one.",
           tid("compare-conclusion"))


def drag_trend(c: Capture, t0: float, t1: float):
    """Drag across the trend chart from retention time t0 to t1 (minutes),
    with the real mouse, then park it; falls back to the chart's own event
    if the drag selected nothing."""
    from selenium.webdriver.common.actions.action_builder import ActionBuilder
    pts = c.js("""
        const gd = document.querySelector('[data-testid=compare-trend]');
        const fl = gd._fullLayout, xa = fl.xaxis, ya = fl.yaxis;
        const b = gd.getBoundingClientRect();
        const y = b.top + fl._size.t + fl._size.h * 0.45;
        return [b.left + fl._size.l + xa.l2p(arguments[0]), b.left + fl._size.l + xa.l2p(arguments[1]), y];""",
               t0, t1)
    x0, x1, y = (int(round(v)) for v in pts)
    ab = ActionBuilder(c.drv)
    pa = ab.pointer_action
    pa.move_to_location(x0, y)
    pa.pointer_down()
    for k in range(1, 11):
        pa.move_to_location(int(x0 + (x1 - x0) * k / 10), y)
    pa.pointer_up()
    ab.perform()
    c.park_mouse()
    try:
        c.wait(lambda: c.exists(tid("compare-annotation-pop")), "the drag to select", 5)
    except CaptureError:
        print("  (the drag selected nothing: using the chart's plotly_selected event)")
        c.js("document.querySelector('[data-testid=compare-trend]')"
             ".emit('plotly_selected', {range: {x: [arguments[0], arguments[1]]}});", t0, t1)


def tut_queue(c: Capture, ids):
    t = "queue"
    sid = ids["diesel"]
    c.reset_storage()
    c.open_samples(f"/samples/{sid}/compare")
    c.compare_ready()
    c.shot(t, "On a final run, click Add to queue at the top right.", tid("add-to-queue"))

    c.click(tid("add-to-queue"))
    c.wait(lambda: c.js("const d = document.querySelector('[data-testid=export-sheet]');"
                        "return !!(d && d.open) && d.querySelector('h2').textContent === 'Add to report queue';"),
           "the Add to report queue sheet")
    c.shot(t, "Pick the standard, or No comparison (chromatogram only), check the report title and click "
              "Add to queue.", tid("export-sheet"))
    c.click(tid("export-queue"))
    c.wait(lambda: c.js("return GCReportQueue.count()") == 1, "the report in the queue")
    c.wait(lambda: not c.js("return document.querySelector('[data-testid=export-sheet]').open"), "the sheet to close")
    c.seed_queue([(ids["final"], "40304"), (ids["released"], "40298")])
    std = c.js("return GCReportQueue.items()[0].standard_name")

    # the queue goes with you: on the Results page, say
    c.drv.get(c.base + "/results")
    c.wait(lambda: c.js("return document.querySelectorAll('[data-testid=results-table] tbody tr').length")
           == len(ids), "the Results table")
    c.wait(lambda: c.js("return document.querySelector('#report-queue-btn .rq-count').textContent") == "3",
           "Report queue 3")
    c.shot(t, "Click Report queue at the top of any page; its number is how many reports are waiting.",
           "#report-queue-btn")

    c.open_queue()
    c.wait(lambda: c.js("return document.querySelectorAll('[data-testid=rq-item]').length") == 3, "three reports")
    first = "[data-testid=rq-item]:nth-child(1) [data-testid=rq-open]"
    c.wait(lambda: c.text(first) == "40329", "40329 first in the queue")
    c.shot(t, "Click a report's lab ID to open that sample in Compare exactly as the report will show it.", first)

    c.click(first)
    c.wait(lambda: c.js("return location.pathname") == f"/samples/{sid}/compare", "40329 in Compare")
    c.wait(lambda: c.js("return !!(window.GCSamples && GCSamples.ready)"), "the Samples page")
    c.wait(lambda: c.text(tid("detail-lab")) == "40329", "40329's header")
    c.compare_ready(std)
    c.shot(t, "Check the sample in Compare: it opens on the report's standard, with the settings it was queued with.",
           [".cmp-toolbar", tid("detail-header")])

    c.open_queue()
    remove = "[data-testid=rq-item]:nth-child(3) [data-testid=rq-remove]"
    c.wait(lambda: c.exists(remove), "Remove on the third report")
    c.shot(t, "Click Remove to take a report out of the queue; Clear empties the whole queue.", remove)
    c.close_dialogs()


def tut_download(c: Capture, ids):
    t = "download"
    sid = ids["diesel"]
    c.reset_storage()
    c.open_samples(f"/samples/{sid}")
    c.overview_ready("40329")
    c.shot(t, "On a final run, click Export report at the top right.", tid("export-report"))

    c.click(tid("export-report"))
    c.wait(lambda: c.js("const d = document.querySelector('[data-testid=export-sheet]');"
                        "return !!(d && d.open) && d.querySelector('h2').textContent === 'Export report';"),
           "the Export report sheet")
    c.wait(lambda: c.js("return document.querySelector('[data-testid=export-download]').classList"
                        ".contains('btn-primary')"), "Download PDF as the main button")
    c.shot(t, "Check Compared with and the report title, then click Download PDF.", tid("export-sheet"))

    c.js("const s = document.querySelector('[data-testid=export-standard]'); s.value = ':none';"
         "s.dispatchEvent(new Event('change'));")
    c.wait(lambda: c.text(tid("export-conclusion")).startswith("No comparison"), "No comparison")
    c.shot(t, "For the chromatogram alone, pick No comparison (chromatogram only) before you download.",
           [".cmp-export .field:has([data-testid=export-standard])", tid("export-conclusion")])
    c.close_dialogs()

    c.seed_queue([(sid, "40329"), (ids["final"], "40304"), (ids["released"], "40298")])
    c.open_queue()
    c.click(tid("rq-download"))
    c.wait(lambda: c.text(tid("rq-msg")).startswith(("Downloaded", "Download failed")), "the ZIP", 180)
    msg = c.text(tid("rq-msg"))
    if not msg.startswith("Downloaded 3 reports"):
        raise CaptureError(f"Download all said: {msg}")
    c.shot(t, "In the report queue, click Download all: several reports come as one ZIP file.", tid("rq-download"))
    c.js("GCReportQueue.clear();")
    c.close_dialogs()

    c.click(tid("more-menu-button"))
    c.wait(lambda: c.exists(tid("act-cdf")), "the ⋯ menu")
    c.shot(t, "For the raw data, open the ⋯ menu (More actions) and click Download CDF.", tid("act-cdf"))
    c.js("document.body.click();")


def tut_upload(c: Capture, ids):
    t = "upload"
    sid = ids["diesel"]
    c.reset_storage()
    c.open_samples(f"/samples/{sid}")
    c.overview_ready("40329")
    c.seed_queue([(sid, "40329"), (ids["final"], "40304"), (ids["released"], "40298")])
    c.open_queue()
    c.shot(t, "Open the report queue and click Upload to QBench.", tid("rq-upload"))

    c.click(tid("rq-upload"))
    c.wait(lambda: c.exists(tid("rq-signin")), "the QBench sign-in")
    c.wait(lambda: c.js("return document.getElementById('rq-qb-user').value") == "lab.user", "the saved username")
    c.wait(lambda: c.text(tid("rq-upload")).startswith("Start upload"), "Start upload")
    c.type_into("#rq-qb-pass", "not-a-real-password")
    c.wait(lambda: c.js("return document.getElementById('rq-qb-pass').type") == "password", "a hidden password")
    c.reveal(tid("rq-signin"))
    c.shot(t, "Check the QBench sign-in, type the password if asked, then click Start upload.", tid("rq-signin"))

    c.click(tid("rq-upload"))
    rows = "return [...document.querySelectorAll('[data-testid=rq-up-item]')].map(r => r.dataset.status);"
    running = ("generating", "report_ok", "uploading")

    def mid_run():
        st = c.js(rows) or []
        if len(st) == 3 and st[0] == "ok" and st[1] in running and c.exists(tid("rq-stop")) \
                and c.js("return document.querySelectorAll('[data-testid=rq-skip]').length") >= 2:
            return st
        return None
    c.wait(mid_run, "the upload to be under way (one sent, one running)", 90, every=0.05)
    c.reveal(tid("rq-progress"))
    c.shot(t, "Watch each report's progress; click Skip to leave one out, or Stop to end the upload.",
           tid("rq-progress"), check=lambda: (c.js(rows) or [None, None])[1] in running)

    c.wait(lambda: c.js("return GCReportQueue.items().every(i => !!i.sent_at)") and not c.exists(tid("rq-stop")),
           "the upload to finish", 120)
    c.wait(lambda: c.js("return [...document.querySelectorAll('[data-testid=rq-item]')]"
                        ".every(li => li.dataset.state === 'sent')"), "Sent to QBench on every report")
    c.js("document.querySelector('.rq-body').scrollTop = 0;")
    # the sidebar's Running now catches up (the upload's task ends a moment after the last report)
    c.wait(lambda: not c.js("return !!document.querySelector('#running-now .rn-glyph-spinner')"),
           "Running now to show the upload ended", 60, soft=True)
    c.shot(t, "Look for Sent to QBench on each report once the upload has finished.", tid("rq-list"))
    c.close_dialogs()


STEPS = {"search": tut_search, "edit": tut_edit, "queue": tut_queue, "download": tut_download,
         "upload": tut_upload}


# ── the run ──────────────────────────────────────────────────────────────────

def write_manifest(out: Path, steps: dict[str, list]) -> dict:
    old = {}
    path = out / "tutorials.json"
    if path.is_file():
        try:
            old = {t["id"]: t for t in json.loads(path.read_text(encoding="utf-8")).get("tutorials", [])}
        except (ValueError, KeyError, TypeError):
            old = {}
    tutorials = []
    for tid_, title, summary in TUTORIALS:
        if tid_ in steps:
            tutorials.append({"id": tid_, "title": title, "summary": summary, "steps": steps[tid_]})
        elif tid_ in old:
            tutorials.append(dict(old[tid_], title=title, summary=summary))
    doc = {"version": 1, "size": [W, H], "tutorials": tutorials}
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    # a box (or the size) on one line: easier to review in a diff
    text = re.sub(r"\[\s+(-?[\d.]+(?:,\s+-?[\d.]+)*)\s+\]",
                  lambda m: "[" + ", ".join(v.strip() for v in m.group(1).split(",")) + "]", text)
    path.write_text(text + "\n", encoding="utf-8")
    return doc


def run(out: Path, only: list[str], keep_png: Path | None) -> None:
    import bootapp
    import ui_compare_harness as harness

    out = out.resolve()
    shots = out / "shots"
    tmp = Path(tempfile.mkdtemp(prefix="gc-learn-"))
    stage = tmp / "stage"                # the new shots, moved into place only when every step worked
    try:
        print(f"building a hub in {tmp} …")
        hub = harness.build(tmp)
        home = tmp / "home"
        home.mkdir(exist_ok=True)
        plan = tmp / "qbench-plan.json"
        plan.write_text(json.dumps(UPLOAD_PLAN), encoding="utf-8")
        login = home / "qbenchlogin.txt"                # the saved sign-in (fake)
        login.write_text("lab.user\nnot-a-real-password\n", encoding="utf-8")
        (home / "qbench.json").write_text(json.dumps({"client_id": "learn-capture",       # the API key (fake)
                                                       "client_secret": "learn-capture"}), encoding="utf-8")
        env = {"GC_TEST_QBENCH_PLAN": str(plan), "QBENCH_LOGIN_FILE": str(login),
               "QBENCH_TOKEN_URL": "http://127.0.0.1:9/oauth2/v1/token"}
        cmd = [sys.executable, str(TESTS / "qbench_stub_boot.py"), "--no-tray"]
        with bootapp.booted(tmp, cmd=cmd, extra_env=env) as (port, _proc, data, _home):
            bootapp.sign_in(port, data)
            downloads = tmp / "downloads"
            downloads.mkdir(exist_ok=True)
            drv = make_driver(downloads)
            try:
                bootapp.browser_sign_in(drv, port)
                cap = Capture(drv, f"http://127.0.0.1:{port}", stage, keep_png)
                for name in only:
                    print(f"{name}:")
                    STEPS[name](cap, hub.ids)
                errors = [e["message"] for e in drv.get_log("browser")
                          if e["level"] == "SEVERE" and "favicon.ico" not in e["message"]]
                if errors:
                    print("browser console errors (check the shots):\n  " + "\n  ".join(errors))
            finally:
                drv.quit()
        # every step worked: replace the old shots of these tutorials (all of them
        # on a full run), then the manifest
        shots.mkdir(parents=True, exist_ok=True)
        full = set(only) == {t[0] for t in TUTORIALS}
        for old in shots.iterdir():
            if old.is_file() and (full or any(old.name.startswith(f"{n}-") for n in only)):
                old.unlink()
        for new in sorted(stage.glob("*.webp")):
            shutil.move(str(new), str(shots / new.name))
        doc = write_manifest(out, cap.steps)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    total = sum(p.stat().st_size for p in shots.glob("*.webp"))
    n = sum(len(t["steps"]) for t in doc["tutorials"])
    print(f"wrote {out / 'tutorials.json'}: {len(doc['tutorials'])} tutorials, {n} steps, "
          f"{total / 1024:.0f} KB of shots")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(ROOT / "static" / "learn"),
                    help="where tutorials.json and shots/ go (default: static/learn)")
    ap.add_argument("--only", default="", help="comma-separated tutorial ids to take (default: all)")
    ap.add_argument("--keep-png", default="", help="also keep Chrome's PNGs in this folder")
    a = ap.parse_args(argv)
    ids = [t[0] for t in TUTORIALS]
    only = [s.strip() for s in a.only.split(",") if s.strip()] or ids
    bad = [s for s in only if s not in ids]
    if bad:
        ap.error(f"unknown tutorial(s) {bad}; known: {ids}")
    only = [i for i in ids if i in only]
    try:
        run(Path(a.out), only, Path(a.keep_png).resolve() if a.keep_png else None)
    except CaptureError as exc:
        print(f"learn_capture: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
