"""v4.0 lane E2, by source (no browser): /admin/hub and /calibration render in
the shell (sidebar, light tokens, running-now footer), so the logo leads home
and no page is a dead end. Hub admin keeps its DOM ids (tests and the task
feed's Open links use them), gets a sub-nav of sections in the order Ryan uses
them, one plain-language intro per section, at most one primary button per
section, and one unlock at the top. Calibration has no password box of its
own: it uses the page's 15-minute unlock."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
T = ROOT / "templates"
JS = ROOT / "static" / "js"

ORDER = ["status", "import-history", "load-folder", "purge-panel", "exports", "diagnostics",
         "presets-panel", "sessions", "server", "hub-address"]
STABLE_IDS = ["load-folder", "import-history", "diagnostics", "purge-panel", "lf-job", "ih-job",
              "purge-job", "btn-lf-stop", "btn-ih-stop", "unlock-form", "pw", "btn-unlock",
              "unlock-msg", "unlocked", "unlocked-left", "btn-lock", "locked-note", "lf-inst",
              "lf-folder", "lf-backfill", "btn-load", "lf-msg", "ih-inst", "ih-last", "ih-processed",
              "ih-csv", "ih-aliases", "btn-ih-dryrun", "btn-ih-start", "ih-msg", "purge-inst",
              "purge-scope", "btn-purge-preview", "purge-preview", "purge-new-path", "purge-confirm",
              "btn-purge-start", "purge-msg", "diag-panel", "diag-options", "diag-total",
              "diag-warning", "btn-diag-download", "btn-diag-estimate", "diag-msg", "exports-rows",
              "exports-msg", "sessions-rows", "sessions-msg", "presets", "preset-new-text",
              "btn-preset-add", "presets-msg", "hub-url", "btn-hub-url", "hub-url-msg",
              "hub-url-effective", "status-tasks", "hub-line"]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _sections(html: str) -> dict:
    """{section id: its markup}, in page order, for <section class="adm-sec" ...>."""
    out = {}
    for m in re.finditer(r'<section class="adm-sec"[^>]*\bid="([^"]+)"(.*?)</section>', html, re.S):
        out[m.group(1)] = m.group(2)
    return out


def test_hub_admin_is_a_shell_page():
    html = _read(T / "hub_admin.html")
    assert html.lstrip().startswith('{% extends "_layout.html" %}')
    assert "<style>" not in html and "<script>" not in html
    assert not re.search(r"\son[a-z]+=", html)
    # scripts the layout already loads are not loaded twice
    for dup in ("js/live.js", "js/running_now.js", "js/session.js", "js/admin_unlock.js"):
        assert dup not in html, dup


def test_hub_admin_keeps_its_ids():
    html = _read(T / "hub_admin.html")
    for i in STABLE_IDS:
        assert len(re.findall(rf'(?<![-\w])id="{re.escape(i)}"', html)) == 1, i
    assert "window.GCAdminCall" in _read(JS / "hub_admin.js")


def test_hub_admin_sections_are_in_ryans_order_with_a_sub_nav():
    html = _read(T / "hub_admin.html")
    secs = _sections(html)
    assert list(secs) == ORDER
    nav = re.search(r'<nav class="adm-nav"[^>]*>(.*?)</nav>', html, re.S).group(1)
    assert re.findall(r'href="#([^"]+)"', nav) == ORDER
    # the unlock is once, at the top (the top bar), before any section
    assert html.index('id="unlock-form"') < html.index('class="adm-sec"')


def test_every_section_has_one_intro_and_at_most_one_primary():
    secs = _sections(_read(T / "hub_admin.html"))
    for sid, body in secs.items():
        intro = re.search(r'<p class="intro">(.*?)</p>', body, re.S)
        assert intro, sid
        words = re.sub(r"<[^>]+>", "", intro.group(1)).split()
        assert 4 <= len(words) <= 30, (sid, len(words))
        assert len(re.findall(r'class="intro"', body)) == 1, sid
        assert len(re.findall(r"\bbtn-primary\b", body)) <= 1, sid
    # JS-built buttons don't add a second primary: the path editor's is inside Results files,
    # which has no primary in its markup
    assert "btn-primary" not in secs["exports"]


def test_no_raw_json_on_the_admin_page():
    shown = re.compile(r"(textContent|innerText|value)\s*=\s*[^;]*JSON\.stringify|"
                       r"\bel\([^;]*JSON\.stringify|createTextNode\([^;]*JSON\.stringify")
    for name in ("hub_admin.js", "purge.js", "diagnostics.js", "admin_page.js"):
        m = shown.search(_read(JS / name))
        assert m is None, (name, m and m.group(0))
    assert "<pre" not in _read(T / "hub_admin.html")


def test_calibration_is_a_shell_page_without_its_own_password_box():
    html = _read(T / "calibration.html")
    assert html.lstrip().startswith('{% extends "_layout.html" %}')
    assert "<script>" not in html and not re.search(r"\son[a-z]+=", html)
    assert 'type="password"' not in html and "admin-pw" not in html
    assert "js/calibration.js" in html
    js = _read(JS / "calibration.js")
    assert "GCAdminUnlock" in js and "admin-pw" not in js
    assert re.search(r"\.(innerHTML|outerHTML)\b|insertAdjacentHTML", js) is None
    for keep in ('id="inst"', 'id="sens"', 'id="btn-save"', 'id="btn-reload"', 'id="rows"',
                 'id="chart"', 'id="status"', 'id="cdf-name"'):
        assert keep in html, keep


def test_the_layout_has_one_unlock_for_every_shell_page():
    layout = _read(T / "_layout.html")
    scripts = re.findall(r"filename='js/([^']+)'", layout)
    assert scripts.index("admin_unlock.js") < scripts.index("shell.js")
    shell = _read(JS / "shell.js")
    assert "GCAdminUnlock" in shell and "gateFor" in shell


def test_the_sidebar_marks_admin_and_the_mark_goes_home():
    sb = _read(T / "_shell.html")
    assert re.search(r'class="sb-mark" href="/"', sb)
    assert re.search(r"nav == 'admin'", sb)


def test_the_classic_page_links_to_the_shell_pages():
    html = _read(T / "index.html")
    for target in ("/instruments", "/admin/hub", "/calibration"):
        assert re.search(r'<a [^>]*href="%s"' % re.escape(target), html), target
    assert "location.href='/instruments'" not in html and "location.href='/calibration'" not in html


def test_other_pages_have_a_way_home():
    for name in ("admin_setup.html", "instruments.html", "sample_link_missing.html"):
        assert 'href="/"' in _read(T / name), name
    for name in ("instruments_home.html", "instrument_detail.html", "setup_guide.html",
                 "instrument_missing.html", "hub_admin.html", "calibration.html"):
        assert _read(T / name).lstrip().startswith('{% extends "_layout.html" %}'), name
    # the login page keeps no sidebar
    assert "_shell.html" not in _read(T / "login.html") and "_layout" not in _read(T / "login.html")


def test_no_page_has_its_own_admin_password_box():
    """ONE unlock mechanism on every page (lane E2 review): no page reads a
    plain password input of its own; they use admin_unlock.js."""
    for name in ("instruments.html", "calibration.html", "hub_admin.html", "index.html"):
        assert 'id="admin-pw"' not in _read(T / name), name
    for name in ("instruments.js", "calibration.js", "app.js"):
        assert "admin-pw" not in _read(JS / name), name
    assert "GCAdminUnlock" in _read(JS / "instruments.js")
    assert "js/admin_unlock.js" in _read(T / "instruments.html")
