"""v5.0 lane R: /results, /settings, /help and the notifications panel.

Source checks (no browser): the pages extend the shell, carry no inline
script, build their DOM with textContent (no innerHTML), parse answers with
GCSession.readJson, never ask for a password with window.prompt, and never
hard-code the D2887/D86 column names (they come from /api/table's
``columns``); the user menu links to the new pages; settings_logic.js's key
lists are settings.py's. Plus a booted app: the pages answer signed in and
send a signed-out browser to the sign-in page, and a changed path key is
still refused by /api/settings (no server behaviour changed).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
for _p in (ROOT, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import settings as settings_mod  # noqa: E402

JS = ROOT / "static" / "js"
TPL = ROOT / "templates"
NEW_JS = ["results_logic.js", "results.js", "settings_logic.js", "settings_page.js",
          "notifications_panel.js", "admin_restart.js"]
PAGES = ["results.html", "settings.html", "help.html"]


def _src(name: str) -> str:
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", (JS / name).read_text(encoding="utf-8"), flags=re.S)


@pytest.mark.parametrize("page", PAGES)
def test_the_pages_extend_the_shell_without_inline_script(page):
    text = (TPL / page).read_text(encoding="utf-8")
    assert text.startswith("{% extends \"_layout.html\" %}")
    assert not re.search(r"<script(?![^>]*\bsrc=)", text), page          # no inline script
    assert "style=" not in text


@pytest.mark.parametrize("name", NEW_JS)
def test_text_only_dom_read_json_and_no_prompt(name):
    src = _src(name)
    assert "innerHTML" not in src and "outerHTML" not in src and "insertAdjacentHTML" not in src, name
    assert "window.prompt" not in src and not re.search(r"(?<![\w.])prompt\(", src), name
    assert ".json()" not in src, name
    if "fetch(" in src:
        assert "GCSession.readJson(" in src, name


def test_no_column_name_is_hard_coded_on_the_results_page():
    """The grouped headers come from /api/table's columns: the page's script
    and template never name a D2887/D86 column themselves."""
    for text in (_src("results.js"), (TPL / "results.html").read_text(encoding="utf-8")):
        assert not re.search(r"['\"](?:2887|D86) (?:IBP|FBP|T\d+)['\"]", text)


def test_the_key_lists_are_the_servers():
    src = (JS / "settings_logic.js").read_text(encoding="utf-8")

    def js_list(name):
        block = re.search(name + r" = \[([^\]]*)\]", src, re.S).group(1)
        return re.findall(r"'([^']+)'", block)
    assert js_list("OPERATOR_KEYS") == list(settings_mod.OPERATOR_KEYS)
    assert js_list("ADMIN_KEYS") == list(settings_mod.ADMIN_KEYS)
    # every field the page edits exists in settings.DEFAULTS
    fields = re.findall(r"\{ key: '([a-z_]+)', section:", src)
    assert fields and all(k in settings_mod.DEFAULTS for k in fields), fields


def test_the_user_menu_opens_the_new_pages():
    shell = (TPL / "_shell.html").read_text(encoding="utf-8")
    assert 'href="/settings"' in shell and 'href="/help"' in shell
    assert "?open=" not in shell
    assert 'href="/results"' in shell and 'data-nav="results"' in shell


def test_the_bell_is_the_panels_and_the_shell_only_opens_it():
    layout = (TPL / "_layout.html").read_text(encoding="utf-8")
    assert "js/notifications_panel.js" in layout and "css/notifications.css" in layout
    shell_js = _src("shell.js")
    assert "/api/notifications" not in shell_js          # one owner of the list
    panel = _src("notifications_panel.js")
    for path in ("/api/notifications", "/dismiss", "/api/notifications/dismiss-all"):
        assert path in panel
    assert "GCLiveAdapter" in panel and "bgFetch" in panel


def test_restart_lives_in_admin():
    admin = (TPL / "hub_admin.html").read_text(encoding="utf-8")
    assert 'id="btn-restart"' in admin and "js/admin_restart.js" in admin and "js/restart.js" in admin
    settings_page = (TPL / "settings.html").read_text(encoding="utf-8")
    assert 'href="/admin/hub#server"' in settings_page
    assert "confirm(" not in _src("admin_restart.js")


def test_the_theme_is_followed():
    """Charts re-theme on GCTheme's event (or <html data-theme>); Settings
    uses GCTheme when the shell provides it."""
    assert "gc:theme" in _src("results.js") and "data-theme" in _src("results.js")
    assert "GCTheme" in _src("settings_page.js")


# ── a booted app ────────────────────────────────────────────────────────────

pytest.importorskip("flask")


def test_the_pages_answer_and_need_a_session(tmp_path):
    from bootapp import booted, get_text, post
    with booted(tmp_path) as (port, _proc, _data, _home):
        for path, testid in (("/results", "results-page"), ("/settings", "settings-page"),
                             ("/help", "help-page")):
            html = get_text(port, path)
            assert f'data-testid="{testid}"' in html, path
            assert 'id="sidebar"' in html and 'id="bell-panel"' in html
            import urllib.request
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            opener = urllib.request.build_opener(NoRedirect)
            try:
                opener.open(req, timeout=5)
                raise AssertionError("a signed-out request was served")
            except urllib.error.HTTPError as e:
                assert e.code == 302 and "/login" in e.headers["Location"], path
        assert 'aria-current="page"' in get_text(port, "/results").split('href="/results"')[1][:200]
        # the settings rules are unchanged: a path is still refused (400)
        code, body = post(port, "/api/settings", {"export_folder": "C:\\elsewhere"})
        assert code == 400 and "export_folder" in body["error"]
        code, _ = post(port, "/api/settings", {"bestfit_threshold": "0.5"})
        assert code == 403                                   # admin key, no password
