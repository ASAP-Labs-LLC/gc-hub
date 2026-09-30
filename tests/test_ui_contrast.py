"""v3.1 (WCAG AA): every text/background pair the new pages use, computed
from ``static/css/tokens.css`` for the light and the dark theme, reaches
4.5:1 (3:1 for the focus ring and the status glyphs, which are not text).
Translucent fills (hover, active, the pills' soft tints) are composited over
the surface they sit on, as the browser does. The rendered pages are checked
the same way in the Selenium smoke (``test_ui_setup_pages_smoke``); this one
needs no browser and catches a token change at once."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOKENS = ROOT / "static" / "css" / "tokens.css"
SHELL = ROOT / "static" / "css" / "shell.css"
BADGE = ROOT / "static" / "css" / "badge.css"


def _blocks(css: str) -> dict:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = {}
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        out[sel.strip()] = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))
    return out


def _themes() -> dict:
    blocks = _blocks(TOKENS.read_text(encoding="utf-8"))
    light = blocks[":root"]
    dark = dict(light, **blocks[':root[data-theme="dark"]'])
    return {"light": light, "dark": dark}


def _resolve(tokens: dict, value: str, depth: int = 0) -> str:
    value = value.strip()
    m = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if m and depth < 10:
        return _resolve(tokens, tokens[m.group(1)], depth + 1)
    return value


def _rgba(text: str) -> tuple:
    text = text.strip()
    if text.startswith("#"):
        h = text[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"rgba?\(([^)]*)\)", text)
    assert m, text
    parts = [float(p) for p in re.split(r"[ ,/]+", m.group(1).strip()) if p]
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0)


def _over(top, bottom):
    a = top[3]
    return tuple(top[i] * a + bottom[i] * (1 - a) for i in range(3)) + (1.0,)


def _lum(c):
    def f(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def ratio(fg, bg) -> float:
    a, b = _lum(fg), _lum(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def colour(tokens, *layers):
    """The colour of stacked layers, bottom first (tokens or literals)."""
    base = (255, 255, 255, 1.0)
    for layer in layers:
        base = _over(_rgba(_resolve(tokens, tokens.get(layer, layer))), base)
    return base


# (foreground, [background layers bottom first], minimum, what)
TEXT = 4.5
UI = 3.0
PAIRS = [
    ("--text", ["--bg"], TEXT, "body text"),
    ("--text", ["--bg-card"], TEXT, "card text"),
    ("--text", ["--sidebar-bg"], TEXT, "sidebar text"),
    ("--text", ["--sidebar-bg", "--bg-active"], TEXT, "active nav item"),
    ("--text", ["--bg-sunken"], TEXT, "text on sunken tracks"),
    ("--text", ["--bg-elevated"], TEXT, "menus and dialogs"),
    ("--text-muted", ["--bg"], TEXT, "captions"),
    ("--text-muted", ["--bg-card"], TEXT, "captions on cards"),
    ("--text-muted", ["--sidebar-bg"], TEXT, "sidebar captions"),
    ("--nav-meta", ["--sidebar-bg", "--bg-hover"], TEXT, "nav meta on hover"),
    ("--nav-meta", ["--sidebar-bg", "--bg-active"], TEXT, "nav meta on the active item"),
    ("--text-muted", ["--bg-elevated"], TEXT, "menu captions"),
    ("--text-muted-sunken", ["--bg-sunken"], TEXT, "muted text on sunken tracks"),
    ("--text-muted-sunken", ["--bg-card", "--bg-sunken"], TEXT, "tiles, pills, notes"),
    ("--pill-final-fg", ["--bg-card", "--good-soft"], TEXT, "Live / final pill"),
    ("--pill-held-fg", ["--bg-card", "--warn-soft"], TEXT, "held pill"),
    ("--pill-error-fg", ["--bg-card", "--bad-soft"], TEXT, "error pill"),
    ("--warn-text", ["--bg"], TEXT, "warning lines"),
    ("--st-error", ["--bg"], TEXT, "error lines"),
    ("--bad", ["--bg"], TEXT, "danger buttons"),
    ("--ink-fg", ["--ink"], TEXT, "primary buttons and the toast"),
    ("--text-inverse", ["--st-error"], TEXT, "the bell's count"),
    ("--text-inverse", ["--bad"], TEXT, "an error toast"),
    ("--badge-fg", ["--bg"], TEXT, "the version badge"),
    ("--chart-axis", ["--bg-card"], TEXT, "chart labels"),
    # v4.0 lane E2: Hub admin and Calibration in the shell (admin.css, calibration.css)
    ("--pill-final-fg", ["--bg"], TEXT, "admin/calibration success lines"),
    ("--pill-final-fg", ["--bg-card"], TEXT, "a finished job's line"),
    ("--st-error", ["--bg-card"], TEXT, "a failed job's line"),
    ("--pill-held-fg", ["--bg-card"], TEXT, "a job's warnings"),
    ("--text-muted", ["--bg-card"], TEXT, "peak numbers"),
    ("--text", ["--bg-card", "--bg-active"], TEXT, "the selected peak"),
    ("--text", ["--bg", "--bg-active"], TEXT, "the admin sub-nav's current item"),
    ("--pill-error-fg", ["--bg-sunken"], TEXT, "an error in the path editor"),
    ("--ink-fg", ["--ink-hover"], TEXT, "primary buttons on hover"),
    # v5.0: the Backfill rows (gc_pages.css): selected and hovered rows, their why line
    ("--text", ["--bg", "--bg-active"], TEXT, "a selected backfill row"),
    ("--text-muted-sunken", ["--bg", "--bg-active"], TEXT, "the why line on a selected row"),
    ("--text-muted-sunken", ["--bg", "--bg-hover"], TEXT, "the why line on a hovered row"),
    ("--text-muted-sunken", ["--bg-card", "--bg-active"], TEXT, "the why line on a selected row (card)"),
    # v5.0.0 lane C: Compare (compare.css) and the report queue sheet (report_queue.css)
    ("--text-muted-sunken", ["--bg", "--bg-sunken"], TEXT, "a marginal finding's badge"),
    ("--text-muted-sunken", ["--bg-card", "--chart-band"], TEXT, "a range band's label"),
    ("--chart-axis", ["--bg"], TEXT, "chart labels full screen"),
    ("--pill-final-fg", ["--bg-elevated"], TEXT, "an uploaded report in the queue sheet"),
    ("--pill-held-fg", ["--bg-elevated"], TEXT, "a partial upload's line"),
    ("--pill-error-fg", ["--bg-elevated"], TEXT, "a failed upload in the queue sheet"),
    ("--text", ["--bg-elevated", "--bg-sunken"], TEXT, "the queue sheet's sign-in-again box"),
    ("--text-muted-sunken", ["--bg-elevated", "--bg-sunken"], TEXT, "the empty queue, sign-in labels"),
    ("--text", ["--bg-elevated", "--bg-hover"], TEXT, "a menu item on hover (Clear annotations)"),
    # v5.0 lane R: Results, Settings, Help, the notifications panel (results.css,
    # settings.css, controls.css, notifications.css)
    ("--st-error", ["--bg-elevated"], TEXT, "an error notification's level"),
    ("--warn-text", ["--bg-elevated"], TEXT, "a warning notification's level"),
    ("--text-muted", ["--bg-elevated"], TEXT, "a notification's time"),
    ("--ink-fg", ["--ink"], TEXT, "a pressed filter chip"),
    ("--pill-final-fg", ["--bg"], TEXT, "a saved setting's line"),
    ("--st-error", ["--bg"], TEXT, "a field's error"),
    ("--text", ["--bg-card"], TEXT, "results numbers"),
    ("--text-muted", ["--bg-card"], TEXT, "a held run's reason and empty cells"),
    ("--text-muted-sunken", ["--bg-card", "--bg-sunken"], TEXT, "backfill and flag tags"),
    ("--text", ["--bg-sunken"], TEXT, "keys on the help page"),
    ("--accent", ["--bg"], UI, "focus ring"),
    ("--accent", ["--sidebar-bg"], UI, "focus ring in the sidebar"),
    ("--accent", ["--bg-sunken"], UI, "focus ring on sunken fields"),
    ("--st-final", ["--bg"], UI, "final glyph"),
    ("--st-held", ["--bg"], UI, "held glyph"),
    ("--st-error", ["--bg"], UI, "error glyph"),
    ("--text-muted", ["--bg"], UI, "never glyph"),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("fg,bg,need,what", PAIRS, ids=[p[3] for p in PAIRS])
def test_token_pair_contrast(theme, fg, bg, need, what):
    t = _themes()[theme]
    back = colour(t, *bg)
    front = _over(_rgba(_resolve(t, t[fg])), back)
    r = ratio(front, back)
    assert r >= need, f"{theme}: {what} ({fg} on {' + '.join(bg)}) is {r:.2f}:1, needs {need}:1"


def test_the_shell_uses_the_checked_tokens():
    css = SHELL.read_text(encoding="utf-8")
    assert re.search(r"\.pill\.final\s*\{[^}]*color:\s*var\(--pill-final-fg\)", css)
    assert re.search(r"\.pill\.held\s*\{[^}]*color:\s*var\(--pill-held-fg\)", css)
    assert re.search(r"\.pill\.error\s*\{[^}]*color:\s*var\(--pill-error-fg\)", css)
    assert re.search(r"\.nav-item \.nav-meta\s*\{[^}]*color:\s*var\(--nav-meta\)", css)


def test_the_focus_ring_is_a_solid_offset_outline():
    css = SHELL.read_text(encoding="utf-8")
    rule = re.search(r"\.gc :focus-visible\s*\{([^}]*)\}", css).group(1)
    assert re.search(r"outline:\s*2px solid var\(--accent\)", rule)
    assert "outline-offset" in rule and "border-radius" not in rule


def test_the_version_badge_is_readable():
    css = BADGE.read_text(encoding="utf-8")
    rule = re.search(r"#app-version\s*\{([^}]*)\}", css).group(1)
    op = re.search(r"opacity:\s*([\d.]+)", rule)
    assert op is None or float(op.group(1)) == 1.0
    assert "var(--badge-fg" in rule


# v4.0 lane E2: the new pages' stylesheets use only the checked tokens for
# colour (no literal colours), and only token pairs listed above for text.
PAGE_CSS = [ROOT / "static" / "css" / "admin.css", ROOT / "static" / "css" / "calibration.css",
            ROOT / "static" / "css" / "compare.css", ROOT / "static" / "css" / "report_queue.css"]
            # v5.0 lane R
            ROOT / "static" / "css" / "results.css", ROOT / "static" / "css" / "settings.css",
            ROOT / "static" / "css" / "controls.css", ROOT / "static" / "css" / "notifications.css"]
CHECKED_FG = {p[0] for p in PAIRS}


@pytest.mark.parametrize("css", PAGE_CSS, ids=lambda p: p.name)
def test_the_new_pages_use_tokens_only(css):
    text = re.sub(r"/\*.*?\*/", "", css.read_text(encoding="utf-8"), flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", text), css.name
    for m in re.finditer(r"(?<![-\w])color\s*:\s*([^;}]+)", text):
        value = m.group(1).strip()
        token = re.fullmatch(r"var\((--[\w-]+)\)", value)
        assert token, (css.name, value)
        assert token.group(1) in CHECKED_FG, (css.name, token.group(1))
