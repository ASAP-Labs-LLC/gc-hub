"""v3.1: headless-Chrome smoke of the new pages (/instruments,
/instruments/<id>, /setup), keyed on ``data-testid``, in the light and dark
themes at 1366x768 (the sidebar is an icon rail) and 1440x900 (full sidebar).
Plus: the setup guide moves on by itself when the agent checks in (the 5 s
fallback poll: this branch has no live.js), and "Add a new GC" asks for the
admin password once and lands on the new instrument's guide.

Skipped when selenium or a Chrome/chromedriver can't be started.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")
webdriver = pytest.importorskip("selenium.webdriver")

import store  # noqa: E402
import ui_setup_demo  # noqa: E402
from bootapp import booted, browser_sign_in, setup_admin  # noqa: E402

SIZES = [(1366, 768), (1440, 900)]
FETCH_SPY = """
window.__gets = [];
const __f = window.fetch;
window.fetch = function (url, opts) {
  const o = opts || {};
  if (!o.method || o.method === 'GET') {
    const h = o.headers || {};
    const bg = typeof h.get === 'function' ? h.get('X-GC-Background') : h['X-GC-Background'];
    window.__gets.push([String(url), bg || null]);
  }
  return __f.apply(this, arguments);
};
"""
THEMES = ["light", "dark"]


def _driver():
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--force-device-scale-factor=1"):
        opts.add_argument(arg)
    opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    try:
        return webdriver.Chrome(options=opts)
    except Exception as exc:  # noqa: BLE001 - no Chrome / driver here
        pytest.skip(f"headless Chrome unavailable: {exc}")


def _wait(pred, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = pred()
        except Exception:  # noqa: BLE001 - DOM not ready yet
            v = None
        if v:
            return v
        time.sleep(0.2)
    return pred()


# WCAG AA on the rendered page: every visible text node's colour (with the
# opacity of its ancestors) against the composited background behind it
# (4.5:1, 3:1 for large text). Disabled controls are exempt, as in WCAG.
CONTRAST_JS = r"""
function parse(c){const m=c.match(/rgba?\(([^)]+)\)/);if(!m)return null;const p=m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);return [p[0],p[1],p[2],p.length>3?p[3]:1];}
function over(t,b){const a=t[3];return [t[0]*a+b[0]*(1-a),t[1]*a+b[1]*(1-a),t[2]*a+b[2]*(1-a),1];}
function lum(c){const f=v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)};return 0.2126*f(c[0])+0.7152*f(c[1])+0.0722*f(c[2]);}
function ratio(a,b){const x=lum(a),y=lum(b);return (Math.max(x,y)+0.05)/(Math.min(x,y)+0.05);}
function bgOf(el){const st=[];let e=el;while(e&&e.nodeType===1){const b=parse(getComputedStyle(e).backgroundColor);if(b&&b[3]>0)st.push(b);if(b&&b[3]>=1)break;e=e.parentElement;}
 let base=parse(getComputedStyle(document.body).backgroundColor)||[255,255,255,1];if(base[3]<1)base=[255,255,255,1];
 for(let i=st.length-1;i>=0;i--)base=over(st[i],base);return base;}
const out=[];const seen=new Set();const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
while(w.nextNode()){const t=w.currentNode;if(!t.textContent.trim())continue;const el=t.parentElement;if(!el||seen.has(el))continue;seen.add(el);
 const r=el.getBoundingClientRect();if(!r.width||!r.height)continue;const cs=getComputedStyle(el);if(cs.visibility==='hidden'||cs.display==='none'||el.closest('[hidden]'))continue;
 let op=1;let e=el;while(e&&e.nodeType===1){op*=parseFloat(getComputedStyle(e).opacity);e=e.parentElement;}
 const fg=parse(cs.color);const bg=bgOf(el);const cr=ratio(over([fg[0],fg[1],fg[2],fg[3]*op],bg),bg);
 const size=parseFloat(cs.fontSize);const large=size>=24||(size>=18.66&&parseInt(cs.fontWeight)>=700);
 if(cr<(large?3:4.5)&&!el.closest('[disabled]'))out.push([t.textContent.trim().slice(0,40),cs.color,op.toFixed(2),cr.toFixed(2)]);}
return out;
"""


def _contrast(drv):
    return _js(drv, CONTRAST_JS)


def _js(drv, script, *args):
    return drv.execute_script(script, *args)


def _tid(drv, testid, attr="length"):
    return _js(drv, f"return document.querySelectorAll('[data-testid=\"{testid}\"]').{attr};")


def _errors(drv):
    try:
        logs = drv.get_log("browser")
    except Exception:  # noqa: BLE001 - not every driver exposes logs
        return []
    return [e["message"] for e in logs if e["level"] == "SEVERE" and "favicon.ico" not in e["message"]]


def _open(drv, port, path, theme, size):
    drv.set_window_size(*size)
    drv.get(f"http://127.0.0.1:{port}/static/favicon.svg")
    _js(drv, "localStorage.setItem('gc.theme', arguments[0]); localStorage.removeItem('gc.sidebar');", theme)
    drv.get(f"http://127.0.0.1:{port}{path}")


def _common(drv, theme, width):
    assert _js(drv, "return document.documentElement.dataset.theme;") == theme
    # no sideways scrolling, the version badge, the shell
    assert _js(drv, "return document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1;")
    assert _js(drv, "return !!document.getElementById('app-version');")
    assert _tid(drv, "sidebar") == 1 and _tid(drv, "user-chip") == 1
    sb = _js(drv, "return document.getElementById('sidebar').getBoundingClientRect().width;")
    if width < 1400:
        assert sb <= 72, sb                  # the icon rail
    else:
        assert sb >= 250, sb
    # the user chip shows the name only (no roles)
    assert _js(drv, "return document.getElementById('user-name').textContent.trim();") == "Test Operator"
    # the dark theme really changes the page's background
    bg = _js(drv, "return getComputedStyle(document.body).backgroundColor;")
    assert (bg == "rgb(255, 255, 255)") == (theme == "light"), bg
    # WCAG AA, including the open user menu
    assert _contrast(drv) == [], (theme, width, drv.current_url)
    _js(drv, "document.getElementById('user-chip').click();")
    assert _contrast(drv) == [], ("menu", theme, width)
    _js(drv, "document.body.click();")
    # the focus ring: a solid 2px outline
    ring = _js(drv, "const b=document.getElementById('user-chip'); b.focus({focusVisible:true});"
                    "const cs=getComputedStyle(b); return [cs.outlineStyle, cs.outlineWidth];")
    assert ring == ["solid", "2px"], ring


def test_the_three_pages_in_both_themes_at_both_sizes(tmp_path):
    hub = ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, _data, _home):
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            for theme in THEMES:
                for size in SIZES:
                    ui_setup_demo.touch_agent(hub.db)

                    # ── /instruments
                    _open(drv, port, "/instruments", theme, size)
                    assert _wait(lambda: _tid(drv, "instrument-card") == 2)
                    labels = _js(drv, "return Object.fromEntries(Array.from(document.querySelectorAll("
                                      "'[data-testid=instrument-card]')).map(c => [c.dataset.instrument, "
                                      "c.querySelector('[data-testid=setup-label]').textContent]));")
                    assert labels == {"gc1": "Ready", "gc2": "Step 4 of 8"}, labels
                    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#feed li').length;") > 0)
                    feed = _js(drv, "return document.getElementById('feed').textContent;")
                    assert "Written to results CSV" in feed and "LEM" not in feed
                    assert _wait(lambda: not _js(drv, "return document.getElementById('nav-setup').hidden;"))
                    assert _js(drv, "return document.getElementById('nav-setup-step').textContent;") == "Step 4"
                    assert _tid(drv, "add-gc") == 1
                    live = _js(drv, "return document.querySelector('[data-instrument=gc1] [data-role=agent-pill]').textContent;")
                    assert live == "Live"
                    _common(drv, theme, size[0])

                    # ── /instruments/gc2
                    _open(drv, port, "/instruments/gc2", theme, size)
                    assert _wait(lambda: _tid(drv, "checklist-step") == 8)
                    statuses = _js(drv, "return Array.from(document.querySelectorAll("
                                        "'[data-testid=checklist-step]')).map(t => t.dataset.status);")
                    assert statuses.count("current") == 1 and statuses[3] == "current", statuses
                    for sec in ("agent", "calibration", "corrections", "export", "methods", "backfill",
                                "conflicts"):
                        assert _tid(drv, "section-" + sec) == 1, sec
                    assert _wait(lambda: "Waiting for the first check-in" in _js(
                        drv, "return document.getElementById('agent-body').textContent;"))
                    assert _js(drv, "return document.querySelectorAll('#corrections-body input').length;") == 12
                    _common(drv, theme, size[0])

                    # ── /setup
                    _open(drv, port, "/calibration?instrument=gc2", theme, size)
                    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#peak-body tr, tbody tr').length;") > 3)
                    time.sleep(0.5)
                    assert _contrast(drv) == [], ("calibration", theme, size)

                    _open(drv, port, "/setup?instrument=gc2", theme, size)
                    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#steps > li').length;") == 8)
                    assert _js(drv, "return document.querySelector('[data-testid=step-checkin]').dataset.status;") == "current"
                    assert _js(drv, "return document.querySelector('[data-testid=guide-step]').textContent;") == "Step 4 of 8"
                    assert _js(drv, "return document.getElementById('picker').value;") == "gc2"
                    _common(drv, theme, size[0])
            assert _errors(drv) == []
        finally:
            drv.quit()


def test_the_guide_moves_on_when_the_agent_checks_in_and_add_a_gc(tmp_path):
    hub = ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            # record every GET the page makes, with its X-GC-Background header
            drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": FETCH_SPY})
            _open(drv, port, "/setup?instrument=gc2", "light", (1440, 900))
            assert _wait(lambda: _js(drv, "return document.querySelector('[data-testid=step-checkin]')"
                                          ".dataset.status;") == "current")
            time.sleep(1.0)
            _js(drv, "window.__settled = window.__gets.length;")
            ui_setup_demo.touch_agent(hub.db, "gc2")               # the agent says hello
            assert _wait(lambda: _js(drv, "return document.querySelector('[data-testid=step-checkin]')"
                                          ".dataset.status;") == "done", timeout=20)
            assert _js(drv, "return document.querySelector('[data-testid=guide-step]').textContent;") == "Step 7 of 8"
            # everything fetched because of the live update or a timer is background
            later = _js(drv, "return window.__gets.slice(window.__settled);")
            assert later and all(bg == "1" for _url, bg in later), later
            assert any("/setup" in url for url, _bg in later), later

            # "Add a new GC": step 1's form, the admin password once, then its guide
            _open(drv, port, "/setup?new=1", "light", (1440, 900))
            assert _wait(lambda: _tid(drv, "new-form") == 1)
            assert _js(drv, "return document.getElementById('picker').value;") == "__new"
            name = drv.find_element("css selector", "[data-testid=new-name]")
            name.send_keys("GC 9")
            assert _js(drv, "return document.querySelector('[data-testid=new-id]').value;") == "gc9"
            _js(drv, "document.querySelector('[data-testid=new-form] button[type=submit]').click();")
            assert _wait(lambda: _js(drv, "return document.getElementById('admin-dialog').open;"))
            drv.find_element("id", "admin-password").send_keys(pw)
            _js(drv, "document.getElementById('admin-ok').click();")
            assert _wait(lambda: "instrument=gc9" in drv.current_url)
            assert _wait(lambda: _js(drv, "return document.querySelectorAll('#steps > li').length;") == 8)
            ev = store.instrument_events.list(instrument_id="gc9", db=hub.db)
            assert [e["kind"] for e in ev] == ["created"] and ev[0]["by"].startswith("Test Operator (")
            # the password is never stored by the page
            stored = _js(drv, "return JSON.stringify(localStorage) + JSON.stringify(sessionStorage);")
            assert pw not in stored
            assert _errors(drv) == []
        finally:
            drv.quit()


def test_step_7_asks_for_the_results_file_first_and_live_updates_keep_what_is_typed(tmp_path):
    hub = ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            # the guide: gc2 has no results file for LEM, so "Go live now" is not offered yet
            _open(drv, port, "/setup?instrument=gc2", "light", (1440, 900))
            assert _wait(lambda: _tid(drv, "step-go_live") == 1)
            assert _tid(drv, "guide-results-path") == 1 and _tid(drv, "guide-keep-hub-only") == 1
            assert _tid(drv, "guide-go-live") == 0
            detail = _js(drv, "return document.querySelector('[data-testid=step-go_live]').textContent;")
            assert "LEM does not read" in detail

            # keep the hub-only file on purpose: then "Go live now" appears
            _js(drv, "window.confirm = () => true;")
            _js(drv, "document.querySelector('[data-testid=guide-keep-hub-only]').click();")
            assert _wait(lambda: _js(drv, "return document.getElementById('admin-dialog').open;"))
            drv.find_element("id", "admin-password").send_keys(pw)
            _js(drv, "document.getElementById('admin-ok').click();")
            assert _wait(lambda: _tid(drv, "guide-go-live") == 1)
            assert [e["kind"] for e in store.instrument_events.list(instrument_id="gc2", db=hub.db)
                    ][0] == "export_hub_only"

            # the instrument page: a half-typed correction survives a live reload
            _open(drv, port, "/instruments/gc2", "light", (1440, 900))
            assert _wait(lambda: _js(drv, "return document.querySelectorAll('#corrections-body input').length;") == 12)
            box = drv.find_element("css selector", "#corrections-body input")
            box.clear()
            box.send_keys("-7.25")
            _js(drv, "document.activeElement.blur();")
            ui_setup_demo.touch_agent(hub.db, "gc2")         # first check-in: the page reloads
            assert _wait(lambda: "Live" in _js(drv, "return document.getElementById('agent-body').textContent;"),
                         timeout=20)
            time.sleep(1.0)
            assert _js(drv, "return document.querySelector('#corrections-body input').value;") == "-7.25"
            assert _errors(drv) == []
        finally:
            drv.quit()
