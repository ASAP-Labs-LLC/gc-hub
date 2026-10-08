"""The analysis report page (v6.0): one Letter page, always.

Pure layout, importable without app.py's side effects (tests render it
straight through xhtml2pdf). ``app._report_html`` calls ``render_html``;
``app._generate_analysis_report_pdf`` asks ``plan`` how tall the two charts
may be and what, if anything, has to be shortened, renders the charts at that
size, then renders the page.

The look is the GC app's (static/css/tokens.css, light theme): ink type on
white, hairline rows, a section title in a left column, colour only for the
deviation fill and the severity of a finding. xhtml2pdf draws it, so the page
is tables and inline styles in the CSS 2.1 subset xhtml2pdf understands
(no flexbox, no rounded corners, cell backgrounds only).

One page, deterministically
---------------------------
``plan`` measures every text block with the real font metrics (reportlab's
``stringWidth``, word-wrapped at the column widths below) and then:

1. gives the charts what is left, between ``CHART_MAX`` and ``CHART_MIN``
   (a short report gets tall charts, a long one shorter charts);
2. if the text does not fit beside the smallest charts, sets the text in the
   next smaller type size (``TYPE_STEPS``);
3. still too long: lets the charts go down to ``CHART_FLOOR``;
4. then lists as many of the conclusion's notes as fit (in their order) and
   one line "N more notes are in the GC hub";
5. then as many marked regions as fit, "N more marked regions are in the
   GC hub";
6. then as many findings as fit (at least one), "N more findings are in the
   GC hub"; room a later step frees goes back to the marked regions, then
   the notes;
7. a conclusion is printed whole up to ``CONCLUSION_KEEP`` characters (the
   cap the hub enforces); only a longer one (an old queue item, an API
   client) is shortened, never below the cap, at a word, ending
   "… (shortened; the full conclusion is in the GC hub)".
8. the footer's Ranges line never leaves a range out (v7: every range on
   the comparison is named on the page): still too long, it says "named
   on the charts" when every range carries its full label on both charts
   (``ranges_on_charts``), else each name is shortened (to as many
   characters as fit, at least ``RANGES_NAME_MIN``; the carbons always
   whole), with the charts allowed down to ``CHART_HARD`` on the way.

With the conclusion at its cap, every section at its worst and the
smallest type, one finding and the footer still fit beside the smallest
charts, in every face ``fonts`` may pick (Segoe UI, Arial, DejaVu Sans,
Bitstream Vera: ``tests/test_report_one_page.py`` renders each). The header
keeps that bound: the title is cut at 90 characters, the lab ID at
``LAB_ID_CHARS`` and the standard's name in the subtitle at
``STD_NAME_CHARS``.

The measurement follows xhtml2pdf's own layout: the same font file and
size, the same column widths, the line height xhtml2pdf uses (``LINE``
ems; every line, the first too), a finding's bold head measured in the bold
face, and words wider than their column broken exactly where ``breakable``
breaks them. What is left between that and the page is a small documented
margin (``MEASURE_MARGIN``, ``SLACK``): cell borders, half-points of
padding and xhtml2pdf's rounding, a few points on a full page.

A word too long for its column (a pasted path, an ID with no spaces) is
broken across lines, as a browser with ``overflow-wrap: anywhere`` would.

The page body is also wrapped in xhtml2pdf's keep-in-frame *shrink*, so a
measurement that is off by a line scales the page by a few percent instead
of spilling onto a second page. Every interpolated string is escaped.
"""
from __future__ import annotations

import html
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ── page geometry (points) ───────────────────────────────────────────────
PAGE_W, PAGE_H = 612.0, 792.0             # US Letter
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 36.0, 32.0, 28.0
BODY_W = PAGE_W - 2 * MARGIN_X            # 540
BODY_H = PAGE_H - MARGIN_TOP - MARGIN_BOTTOM
LABEL_W = 92.0                            # the section-title column (its last 8pt the gap);
SECTION_LABELS = ("Findings", "Marked regions", "Conclusion")   # each on one line in it
KEY_W = 200.0                             # a chart's legend
TITLE_W = BODY_W - 192.0                  # the title column (the lab ID takes 180 + gap)
LOGO_W = 74.0                             # a logo and its gap, when there is one
TAG_W = 56.0                              # a finding's severity tag
TEXT_W = BODY_W - LABEL_W                 # a section's text column
FINDING_W = TEXT_W - TAG_W - 6.0
IMG_PX_PER_PT = 1 / 0.75                  # xhtml2pdf reads an <img> width in CSS px

CHART_MAX = (284.0, 178.0)                # trend, difference
CHART_MIN = (150.0, 96.0)
CHART_FLOOR = (112.0, 72.0)               # the smallest, once the type is at its smallest
TYPE_STEPS = (8.5, 7.8, 7.2, 6.8)         # body type, largest first
FOOT_FS = 6.5
LINE = 1.38                               # line height, in ems
SLACK = 10.0                              # points kept back: the model is within ~6pt of xhtml2pdf
MEASURE_MARGIN = 1.0                      # no proportional margin: the model is exact to a few points
# xhtml2pdf's layout, measured (constant across faces and sizes):
PARA = 2.0                                # the space xhtml2pdf adds to every paragraph
ROW = 4.0 + 2.0 + PARA                    # a section row: 4pt above, 2pt below, the paragraph space
SECTION = 4.0                             # between two sections (the 0.75pt rule and its space)
FOOT_LINE = 1.35                          # the footer's line height (``.foot td``)
FOOT_TOP = 10.0 + 4.0                     # the footer: its margin, the first row's extra padding
LAB_ID_CHARS = 40                         # the header's lab ID, at most (real ones are ~5-12)
STD_NAME_CHARS = 120                      # the standard's name in the subtitle, at most
RANGES_NAME_MIN = 6                       # a range's name in the footer, when shortened
CHART_HARD = (100.0, 64.0)                # the charts' last floor: for a long Ranges line only
CONCLUSION_KEEP = 1500                    # the conclusion cap: always printed whole

# ── tokens (static/css/tokens.css, light) ────────────────────────────────
INK = "#0f172a"
MUTED = "#64748b"
MUTED_SUNKEN = "#5b6678"
BORDER = "#d4d8dd"
HAIRLINE = "#e7e9ed"
SUNKEN = "#f4f4f5"
DEV_ABOVE, DEV_ABOVE_FILL = "#dc2626", "#fecaca"
DEV_BELOW, DEV_BELOW_FILL = "#2563eb", "#bfdbfe"
CHART_REF = "#a8b0bc"

# ── the charts' marks (v7): colour for meaning, texture for print ────────
# Every mark is told apart twice, by colour and by something that survives
# a black-and-white printer: the sample is the one coloured line (the app's
# accent, dark: ~35% grey in greyscale) over the standard's light grey area
# (~88% grey, with a mid-grey edge); "higher" and "lower" are red and blue
# fills (both ~85% grey once printed, so the colour alone would vanish)
# carrying opposite textures, diagonal hatch and dots, in their dark tone;
# spikes are triangles pointing the way they go; the thresholds differ by
# dash; range bands are a neutral tint with hairline edges (no hue: colour
# is kept for what the data means).
SAMPLE = "#0e7490"                         # --accent
STD_EDGE = "#8f98a6"                       # the standard's outline: between --chart-ref and --chart-axis
STD_FILL = (168, 176, 188, 0.35)           # --chart-ref at 35%
BAND_FILL = (15, 23, 42, 0.05)
BAND_EDGE = (15, 23, 42, 0.22)
ABOVE_PATTERN, BELOW_PATTERN = "/", "."    # Plotly fillpattern shapes
PATTERN_PT = 3.2                           # a pattern cell, printed points
PATTERN_SOLIDITY = {"/": 0.22, ".": 0.3}
THRESHOLD_DASH = {"marginal": "dot", "moderate": "dash", "significant": "longdashdot"}
BAND_LABEL_FS = 6.5                        # range labels on both charts (pt)
BAND_LABEL_PAD = 3.0                       # from the band's left edge (pt)
BAND_ROW_PT = BAND_LABEL_FS * 1.25         # one row of range labels
BAND_LANE_PAD = 1.5                         # above and below the label rows


def band_lane_pt(rows: int) -> float:
    """The data-free strip *rows* rows of range labels sit in (0: none)."""
    return rows * BAND_ROW_PT + 2 * BAND_LANE_PAD if rows else 0.0


def rgba(c: tuple) -> str:
    return f"rgba({c[0]},{c[1]},{c[2]},{c[3]:g})"


def band_labels(windows: list[dict], x0: float, x1: float,
                plot_w_pt: float) -> list[tuple[dict, str, float, int]]:
    """The range labels, one decision for both charts (they share the
    width and the x axis): ``(window, text, x, row)`` per labelled window
    (``x`` in minutes: a label's left edge, or a tag's centre).

    Left to right, each label starts at its band's left edge, or past the
    end of an earlier band that overlaps it (so no edge runs through the
    text), and is the first of "<label> Cx–Cy", "<label>", "Cx–Cy" and "Cx–y" that fits
    in the band from there without touching the previous label (row 0).
    A band too narrow for any of them gets its tag, the range's number
    (``index + 1``; the footer's Ranges line names it: ``range_tag``),
    centred on the band, in row 0 or, when that would touch a label, row 1.
    Only a band with neither is left unlabelled (the footer still names it)."""
    span = max(x1 - x0, 1e-9)

    def pt(t: float) -> float:
        return (min(max(float(t), x0), x1) - x0) / span * plot_w_pt
    out: list[tuple[dict, str, float, int]] = []
    ends = [-1e9, -1e9]                    # where each row's last label ends (pt)
    seen: list[dict] = []
    for i, w in enumerate(windows):
        if not w.get("evaluable"):
            continue
        t0, t1 = float(w["t0"]), float(w["t1"])
        start = t0
        for o in seen:                     # clear of earlier bands' right edges
            if float(o["t0"]) <= start < float(o["t1"]) < t1:
                start = float(o["t1"])
        seen.append(w)
        a, b = pt(start), pt(t1)
        cs, ce = w["c_start"], w["c_end"]
        carbons = f"C{cs}–C{ce}" if cs != ce else f"C{cs}"
        compact = f"C{cs}–{ce}" if cs != ce else f"C{cs}"
        label = str(w.get("label") or "").strip()
        # the name matters more than the carbons (the findings give those):
        # "<label> Cx–Cy", then the label alone, then the carbons
        tiers = ([f"{label} {carbons}", label] if label else []) + [carbons, compact]
        placed = False
        for text in dict.fromkeys(tiers):
            need = line_width(text, BAND_LABEL_FS) + 2 * BAND_LABEL_PAD
            if b - a >= need and a >= ends[0]:
                out.append((w, text, max(start, x0), 0))
                ends[0] = a + need
                placed = True
                break
        if placed:
            continue
        tag = range_tag(w, i)
        half = line_width(tag, BAND_LABEL_FS) / 2 + 1.0
        mid = (pt(t0) + b) / 2
        lo_, hi_ = mid - half, mid + half
        if lo_ < 0 or hi_ > plot_w_pt:
            continue
        for row in (0, 1):
            if lo_ >= ends[row]:
                out.append((w, tag, x0 + mid / plot_w_pt * span, row))
                ends[row] = hi_ + 1.0
                break
    return out


def range_tag(w: dict, i: int) -> str:
    """A range's tag on the charts: its number on the comparison."""
    return str(int(w.get("index", i)) + 1)


def is_tag(text: str) -> bool:
    return text.isdigit()


# ── legend swatches: tiny PNGs drawn here (no image library) ─────────────
_SW_PX = 4                                 # swatch pixels per point

def _png_rgb(rows: list[bytearray], w: int, h: int) -> bytes:
    import struct
    import zlib

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _over_white(c: tuple) -> tuple[int, int, int]:
    a = c[3]
    return tuple(round(255 * (1 - a) + v * a) for v in c[:3])


def swatch_png(kind: str, w_pt: float = 12.0, h_pt: float = 7.0) -> bytes:
    """A legend swatch matching the chart's mark: ``standard`` (grey area,
    edge on top), ``sample`` (the coloured line), ``range`` (tint, hairline
    edges), ``above`` (red fill, diagonal hatch), ``below`` (blue fill, dots)."""
    k = _SW_PX
    w, h = round(w_pt * k), round(h_pt * k)
    white = (255, 255, 255)
    px = [[white] * w for _ in range(h)]

    def fill(c, x0=0, y0=0, x1=w, y1=h):
        for y in range(max(y0, 0), min(y1, h)):
            for x in range(max(x0, 0), min(x1, w)):
                px[y][x] = c
    cell = round(PATTERN_PT * k)
    if kind == "standard":
        top = round(h * 0.3)
        fill(_over_white(STD_FILL), y0=top)
        fill(_hex(STD_EDGE), y0=top, y1=top + max(1, round(0.6 * k)))
    elif kind == "sample":
        t = round(1.0 * k)
        fill(_hex(SAMPLE), y0=(h - t) // 2, y1=(h - t) // 2 + t)
    elif kind == "range":
        fill(_over_white(BAND_FILL))
        edge = _over_white(BAND_EDGE)
        e = max(1, round(0.6 * k))
        fill(edge, x1=e)
        fill(edge, x0=w - e)
    elif kind in ("above", "below"):
        bg, fg = ((_hex(DEV_ABOVE_FILL), _hex(DEV_ABOVE)) if kind == "above"
                  else (_hex(DEV_BELOW_FILL), _hex(DEV_BELOW)))
        fill(bg)
        if kind == "above":                # "/" lines, as Plotly draws them
            t = max(1.0, PATTERN_SOLIDITY["/"] * cell)
            for y in range(h):
                for x in range(w):
                    if ((x + y) % cell) < t:
                        px[y][x] = fg
        else:                              # dots on a square grid
            r = max(1.0, (PATTERN_SOLIDITY["."] * cell * cell / math.pi) ** 0.5)
            for y in range(h):
                for x in range(w):
                    dx = (x % cell) - cell / 2 + 0.5
                    dy = (y % cell) - cell / 2 + 0.5
                    if dx * dx + dy * dy <= r * r:
                        px[y][x] = fg
    else:
        raise ValueError(kind)
    rows = [bytearray(b for p in row for b in p) for row in px]
    return _png_rgb(rows, w, h)

FAMILY = "GCReport"
NO_DEVIATION_FALLBACK = "No deviations detected."


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


# ── fonts ─────────────────────────────────────────────────────────────────
def _font_candidates() -> list[tuple[str, str, str]]:
    """``(regular, bold, italic)`` TrueType files, the app's own face first.

    The app's type is the system stack (Segoe UI on ASAPSV1); the report
    embeds the first family present, never fetching anything: Segoe UI
    (bold is Semibold, the app's 600 weight), Arial (Windows, macOS),
    DejaVu Sans (Linux) and reportlab's own Bitstream Vera (always there)."""
    win = Path(os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows") / "Fonts"
    mac = Path("/System/Library/Fonts/Supplemental")
    dejavu = Path("/usr/share/fonts/truetype/dejavu")
    out = [
        (win / "segoeui.ttf", win / "seguisb.ttf", win / "segoeuii.ttf"),
        (win / "segoeui.ttf", win / "segoeuib.ttf", win / "segoeuii.ttf"),
        (win / "arial.ttf", win / "arialbd.ttf", win / "ariali.ttf"),
        (mac / "Arial.ttf", mac / "Arial Bold.ttf", mac / "Arial Italic.ttf"),
        (dejavu / "DejaVuSans.ttf", dejavu / "DejaVuSans-Bold.ttf", dejavu / "DejaVuSans-Oblique.ttf"),
    ]
    try:
        import reportlab
        rl = Path(reportlab.__file__).parent / "fonts"
        out.append((rl / "Vera.ttf", rl / "VeraBd.ttf", rl / "VeraIt.ttf"))
    except ImportError:
        pass
    return [tuple(str(p) for p in c) for c in out]


_FONTS: dict = {}


def fonts() -> dict:
    """Register the report's family with reportlab (once per process) and
    tell xhtml2pdf about it. ``{regular, bold, italic, family, file}``; the
    built-in Helvetica when no TrueType file could be read (it cannot draw
    ``≥`` or ``−``, so that is the last resort)."""
    if _FONTS:
        return _FONTS
    fallback = {"regular": "Helvetica", "bold": "Helvetica-Bold",
                "italic": "Helvetica-Oblique", "family": "Helvetica", "file": None}
    try:
        from reportlab.lib.fonts import addMapping
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:
        _FONTS.update(fallback)
        return _FONTS
    for reg, bold, ital in _font_candidates():
        if not (os.path.isfile(reg) and os.path.isfile(bold)):
            continue
        ital = ital if os.path.isfile(ital) else reg
        # reportlab keeps the FIRST TrueType font registered under a name
        # (registerFont ignores a second one), so a face chosen after
        # another (the tests' fixture switching faces) would be measured
        # and drawn in the first; drop ours before registering
        for name in (FAMILY, FAMILY + "-Bold", FAMILY + "-Italic"):
            getattr(pdfmetrics, "_fonts", {}).pop(name, None)
        try:
            pdfmetrics.registerFont(TTFont(FAMILY, reg))
            pdfmetrics.registerFont(TTFont(FAMILY + "-Bold", bold))
            pdfmetrics.registerFont(TTFont(FAMILY + "-Italic", ital))
        except Exception:
            continue
        addMapping(FAMILY, 0, 0, FAMILY)
        addMapping(FAMILY, 1, 0, FAMILY + "-Bold")
        addMapping(FAMILY, 0, 1, FAMILY + "-Italic")
        addMapping(FAMILY, 1, 1, FAMILY + "-Bold")
        try:
            import xhtml2pdf.default
            xhtml2pdf.default.DEFAULT_FONT[FAMILY.lower()] = FAMILY
        except ImportError:
            pass
        _FONTS.update(regular=FAMILY, bold=FAMILY + "-Bold", italic=FAMILY + "-Italic",
                      family=FAMILY, file=reg)
        return _FONTS
    _FONTS.update(fallback)
    return _FONTS


def chart_font_family() -> str:
    """The charts' CSS font stack (Plotly in kaleido's Chrome): the app's."""
    return "'Segoe UI', Arial, 'Helvetica Neue', Helvetica, sans-serif"


# ── measuring ────────────────────────────────────────────────────────────
def _width(text: str, font: str, size: float) -> float:
    try:
        from reportlab.pdfbase import pdfmetrics
        return pdfmetrics.stringWidth(text, font, size)
    except Exception:
        return len(text) * size * 0.55


def line_width(text: str, size: float, bold: bool = False) -> float:
    """Points one line of *text* takes in the report's face."""
    f = fonts()
    return _width(str(text), f["bold"] if bold else f["regular"], size)


def line_count(text: str, width: float, size: float, bold: bool = False,
               head: str = "") -> int:
    """Lines *text* takes in a column *width* points wide, as xhtml2pdf sets
    it: words wider than the column first broken where ``breakable`` breaks
    them (the page prints that text), then a greedy word wrap. *head*: a
    bold run before the text on its first line (a finding's range)."""
    f = fonts()
    font = f["bold"] if bold else f["regular"]
    text = breakable(text, width, size, bold)
    words_head = [(w, f["bold"]) for w in str(head).split()]
    total = 0
    for i, para in enumerate(str(text).split("\n")):
        words = (words_head if i == 0 else []) + [(w, font) for w in para.split()]
        if not words:
            total += 1
            continue
        lines, cur = 1, 0.0
        for w, wfont in words:
            ww = _width(w, wfont, size)
            if ww > width:                         # only a word breakable could not split
                extra = math.ceil(ww / width)
                lines += extra if cur else extra - 1
                cur = ww - (extra - 1) * width
                continue
            need = ww if cur == 0 else cur + _width(" ", wfont, size) + ww
            if need <= width:
                cur = need
            else:
                lines += 1
                cur = ww
        total += lines
    return total


def breakable(text: str, width: float, size: float, bold: bool = False) -> str:
    """*text* with every word wider than *width* split into pieces that fit
    (xhtml2pdf never breaks inside a word, it overflows the column)."""
    f = fonts()
    font = f["bold"] if bold else f["regular"]
    out_lines = []
    for para in str(text).split("\n"):
        words = []
        for w in para.split(" "):
            if not w or _width(w, font, size) <= width:
                words.append(w)
                continue
            piece = ""
            for ch in w:
                if piece and _width(piece + ch, font, size) > width:
                    # break after the last hyphen, slash or underscore if there is one
                    cut = max(piece.rfind(x) for x in "-/_.")
                    if cut > 0:
                        words.append(piece[:cut + 1])
                        piece = piece[cut + 1:]
                    else:
                        words.append(piece)
                        piece = ""
                piece += ch
            words.append(piece)
        out_lines.append(" ".join(words))
    return "\n".join(out_lines)


def _clip(text: str, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _block_h(lines: int, size: float) -> float:
    return lines * size * LINE


# ── content model ─────────────────────────────────────────────────────────
_SEV = re.compile(r"— (significant|moderate|marginal)\b")
_HEAD = re.compile(r"^(.{1,80}?\(C\d+–C\d+[^)]*\)):\s|^([^:]{1,60}):\s")


def finding_rows(bullets_text: str) -> list[dict]:
    """The findings, one row per line of the server's bullet text (the line
    printed whole, never reworded): ``{head, rest, severity}``. ``head`` is
    the range part (``Label (Cx–Cy):``), set in the semibold; ``severity``
    the first ``— <severity>`` in the line, shown as a tag as well."""
    rows = []
    for raw in (bullets_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        bullet = line.startswith("•")
        text = line.lstrip("•").strip() if bullet else line
        head, rest = "", text
        if bullet:
            m = _HEAD.match(text)
            if m:
                head = (m.group(1) or m.group(2)) + ":"
                rest = text[m.end():]
        sev = _SEV.search(text)
        rows.append({"head": head, "rest": rest, "text": text,
                     "severity": sev.group(1) if sev else None})
    return rows


@dataclass
class Plan:
    """What fits: the charts' heights (points), the body type size, and what
    was shortened (``None`` = nothing; ``shortened`` says it in words)."""
    trend_h: float
    diff_h: float
    fs: float
    findings_shown: int | None = None      # None: all
    regions_shown: int | None = None
    notes_shown: int | None = None
    conclusion_chars: int | None = None
    ranges_name_chars: int | None = None   # each range's name shortened to this
    ranges_on_charts: bool = False         # the Ranges line says "named on the charts"
    shortened: list = field(default_factory=list)

    @property
    def width(self) -> float:
        return BODY_W

    @property
    def note_fs(self) -> float:
        """The Notes sub-block's type: a step under the conclusion's."""
        return round(self.fs * 0.9, 2)


def _shorten(text: str, limit: int, tail: str) -> str:
    if limit is None or len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.—–-")
    return f"{cut}… {tail}"


CONCLUSION_TAIL = "(shortened; the full conclusion is in the GC hub)"


def _more(n: int, one: str, many: str) -> str:
    return (one if n == 1 else many).format(n=n)


def more_regions_text(n: int, shown: int = 1) -> str:
    if not shown:
        return _more(n, "1 marked region is in the GC hub.", "{n} marked regions are in the GC hub.")
    return _more(n, "1 more marked region is in the GC hub.",
                 "{n} more marked regions are in the GC hub.")


def more_notes_text(n: int, shown: int = 1) -> str:
    if not shown:
        return _more(n, "1 note is in the GC hub.", "{n} notes are in the GC hub.")
    return _more(n, "1 more note is in the GC hub.", "{n} more notes are in the GC hub.")


def more_findings_text(n: int) -> str:
    return _more(n, "1 more finding is in the GC hub.", "{n} more findings are in the GC hub.")


def _footer_parts(line: str) -> tuple[str, str]:
    head, sep, rest = line.partition(": ")
    return (head, rest) if sep and len(head) <= 14 else ("", line)


_RANGE_ENTRY = re.compile(r"(.*?)\s*(C\d+(?:–C\d+)?)(?:, |$)")


def range_entries(rest: str) -> list[tuple[str, str]]:
    """The Ranges line's entries, ``(name, carbons)``: "Gas C5–C11, Oil
    C20–C44" → ``[("Gas", "C5–C11"), ("Oil", "C20–C44")]``."""
    return [(n, c) for n, c in _RANGE_ENTRY.findall(rest or "")] if rest != "none" else []


def ranges_text(rest: str, plan: "Plan") -> str:
    """The Ranges line as printed: every range, always (``plan`` may say
    they are named on the charts, or shorten each name, never drop one)."""
    entries = range_entries(rest)
    if plan.ranges_on_charts and entries:
        n = len(entries)
        return f"{n} range{'s' if n != 1 else ''}, each named on both charts"
    k = plan.ranges_name_chars
    if k is None or not entries:
        return rest

    def short(name: str) -> str:            # the "(6) " tag stays whole
        tag, name = re.match(r"(\(\d+\) )?(.*)", name, re.S).groups()
        return (tag or "") + (name if len(name) <= k else name[:k].rstrip() + "…")
    return ", ".join(f"{short(n)} {c}".strip() for n, c in entries)


def _rows_h(lines: list[str], shown: int | None, fs: float, more_extra: float = 0.0) -> float:
    """Section rows, one per line (and the "N more" row): each its text's
    lines plus ``ROW`` (the cell's padding and xhtml2pdf's paragraph space)."""
    view = lines if shown is None else lines[:shown]
    h = sum(_block_h(line_count(x, TEXT_W, fs), fs) + ROW for x in view)
    if len(view) < len(lines):
        h += _block_h(1, fs) + ROW + more_extra
    return h


def _text_height(plan: Plan, *, title: str, subtitle: str, lab_id: str, std_name: str,
                 rows: list[dict], conclusion: str, region_lines: list[str],
                 note_lines: list[str], footer_lines: list[str],
                 has_logo: bool = False) -> float:
    """The page's height without the two chart images, modelled on the
    page's own CSS (see ``_css``) and xhtml2pdf's layout of it."""
    fs = plan.fs
    h = 0.0
    title_w = TITLE_W - (LOGO_W if has_logo else 0)
    # header: title + subtitle at left (wraps), lab ID + date at right, the rule
    left = _block_h(line_count(title, title_w, 15, True), 15) + \
        _block_h(line_count(subtitle, title_w, 8), 8)
    right = 9 + _block_h(line_count(display_lab_id(lab_id), 176, 13, True), 13) + 12
    h += max(left, right, 42) + 12 + 1.5
    # chart chrome (title rows and gaps), both charts; a long caption wraps
    for verb in ("against", "minus"):
        cap = "Difference  " + chart_caption(lab_id, std_name, verb)
        h += _block_h(line_count(cap, BODY_W - KEY_W, fs), fs) + 26
    # findings: one row each (a bold head, then the text beside the severity tag)
    shown = rows if plan.findings_shown is None else rows[:plan.findings_shown]
    for r in shown or [{"text": NO_DEVIATION_FALLBACK}]:
        h += _block_h(line_count(r.get("rest", r["text"]), FINDING_W, fs,
                                 head=r.get("head", "")), fs) + ROW
    if plan.findings_shown is not None and plan.findings_shown < len(rows):
        h += _block_h(1, fs) + ROW
    h += SECTION
    # marked regions (their own section, between the findings and the conclusion)
    if region_lines:
        h += _rows_h(region_lines, plan.regions_shown, fs) + SECTION
    # the conclusion's row, then its notes' row (a heading and the lines, a step smaller)
    concl = _shorten(conclusion, plan.conclusion_chars, CONCLUSION_TAIL)
    h += _block_h(line_count(concl or "No conclusion available.", TEXT_W, fs), fs) + ROW
    if note_lines:
        nfs = plan.note_fs
        view = note_lines if plan.notes_shown is None else note_lines[:plan.notes_shown]
        h += ROW + _block_h(1, nfs) + 1 + PARA
        h += sum(_block_h(line_count(x, TEXT_W, nfs), nfs) + 1.5 + PARA for x in view)
        if len(view) < len(note_lines):
            h += _block_h(1, nfs) + 1.5 + PARA
    # footer: 10pt above, the first row's 5pt top padding, then rows of 1pt+1pt
    foot = FOOT_TOP
    for line in footer_lines:
        head, rest = _footer_parts(line)
        if head == "Ranges":
            rest = ranges_text(rest, plan)
        foot += line_count(rest, TEXT_W, FOOT_FS) * FOOT_FS * FOOT_LINE + 2 + PARA
    foot += FOOT_FS * FOOT_LINE + 2 + PARA
    h += foot
    return h * MEASURE_MARGIN + SLACK


def split_comments(comments: list[dict]) -> tuple[list[dict], list[dict]]:
    """``(marked regions, notes)``: an annotation comment (``t0`` and ``t1``
    set) is a marked region; any other comment is an earlier note."""
    regions = [c for c in comments or [] if c.get("t0") is not None and c.get("t1") is not None]
    notes = [c for c in comments or [] if c.get("t0") is None or c.get("t1") is None]
    return regions, notes


def plan(*, doc_name: str, lab_id: str, std_name: str, bullets_text: str,
         conclusion: str, region_lines: list[str] = (), note_lines: list[str] = (),
         footer_lines: list[str], has_logo: bool = False,
         ranges_on_charts: bool = False) -> Plan:
    """The one-page plan for this content (see the module docstring)."""
    title = display_title(doc_name)
    subtitle = subtitle_text(std_name)
    rows = finding_rows(bullets_text)
    region_lines = list(region_lines or [])
    note_lines = list(note_lines or [])
    kw = dict(title=title, subtitle=subtitle, lab_id=lab_id, std_name=std_name, rows=rows,
              conclusion=conclusion or "", region_lines=region_lines, note_lines=note_lines,
              footer_lines=list(footer_lines or []), has_logo=has_logo)
    ratio = CHART_MAX[1] / CHART_MAX[0]
    floor = {"charts": CHART_MIN}

    def min_charts() -> float:
        return floor["charts"][0] + floor["charts"][1]

    def fits(p: Plan) -> bool:
        return BODY_H - _text_height(p, **kw) >= min_charts()

    def cut(p: Plan, attr: str, total: int, floor: int = 0) -> None:
        """List as many as fit (at least *floor*)."""
        setattr(p, attr, total)
        while getattr(p, attr) > floor and not fits(p):
            setattr(p, attr, getattr(p, attr) - 1)

    def give_back(p: Plan, attr: str, total: int) -> None:
        """Room freed by a later step: list more again while they fit."""
        n = getattr(p, attr)
        if n is None:
            return
        while n < total:
            setattr(p, attr, n + 1)
            if not fits(p):
                setattr(p, attr, n)
                return
            n += 1
        setattr(p, attr, None)

    p = Plan(trend_h=CHART_MIN[0], diff_h=CHART_MIN[1], fs=TYPE_STEPS[0])
    for fs in TYPE_STEPS:                         # 1-2: the type steps down
        p.fs = fs
        if fits(p):
            break
    if not fits(p):                                        # 3: smaller charts
        floor["charts"] = CHART_FLOOR
    if not fits(p) and note_lines:                         # 4: notes
        cut(p, "notes_shown", len(note_lines))
    if not fits(p) and region_lines:                       # 5: marked regions
        cut(p, "regions_shown", len(region_lines))
    if not fits(p) and len(rows) > 1:                      # 6: findings
        cut(p, "findings_shown", len(rows), floor=1)
    if not fits(p) and len(conclusion or "") > CONCLUSION_KEEP:   # 7: an over-cap conclusion
        lo, hi = CONCLUSION_KEEP, len(conclusion)
        while lo < hi:                                     # the longest that fits
            mid = (lo + hi + 1) // 2
            p.conclusion_chars = mid
            if fits(p):
                lo = mid
            else:
                hi = mid - 1
        p.conclusion_chars = lo
    range_names = [n for line in kw["footer_lines"] if _footer_parts(line)[0] == "Ranges"
                   for n, _c in range_entries(_footer_parts(line)[1])]
    longest = max((len(n) for n in range_names), default=0)

    def shorten_names(lowest: int) -> None:
        lo, hi = lowest, longest - 1
        p.ranges_name_chars = lowest
        if not fits(p):
            return
        while lo < hi:                                     # the longest names that fit
            mid = (lo + hi + 1) // 2
            p.ranges_name_chars = mid
            if fits(p):
                lo = mid
            else:
                hi = mid - 1
        p.ranges_name_chars = lo
    if not fits(p) and range_names:                        # 8: the Ranges line, never a range left out
        if ranges_on_charts:
            p.ranges_on_charts = True
        elif longest > 16:
            shorten_names(16)
        if not fits(p) and not p.ranges_on_charts:
            floor["charts"] = CHART_HARD
            if not fits(p) and longest > RANGES_NAME_MIN:
                shorten_names(RANGES_NAME_MIN)
            elif fits(p) and p.ranges_name_chars is not None:
                shorten_names(16)                          # the lower floor bought room back
    give_back(p, "regions_shown", len(region_lines))       # regions first, then notes
    give_back(p, "notes_shown", len(note_lines))
    if p.ranges_on_charts:
        p.shortened.append("ranges named on the charts")
    if p.ranges_name_chars is not None:
        p.shortened.append(f"range names shortened to {p.ranges_name_chars} characters")
    for attr, total, what in (("notes_shown", len(note_lines), "notes"),
                              ("regions_shown", len(region_lines), "marked regions"),
                              ("findings_shown", len(rows), "findings")):
        if getattr(p, attr) is not None:
            p.shortened.append(f"{what}: {getattr(p, attr)} of {total} listed")
    if p.conclusion_chars is not None:
        p.shortened.append(f"conclusion shortened to {p.conclusion_chars} of "
                           f"{len(conclusion)} characters")
    if not fits(p):
        p.shortened.append("does not fit even at the smallest; the page is scaled to fit")
    lo = floor["charts"]
    if lo in (CHART_FLOOR, CHART_HARD):
        p.shortened.append("charts below their usual minimum")
    room = max(BODY_H - _text_height(p, **kw), min_charts())
    trend = min(CHART_MAX[0], max(lo[0], room / (1 + ratio)))
    diff = min(CHART_MAX[1], max(lo[1], room - trend))
    p.trend_h, p.diff_h = round(trend), round(diff)
    return p


def display_title(doc_name: str) -> str:
    t = " ".join(str(doc_name or "GC Analysis").split()) or "GC Analysis"
    return t if len(t) <= 90 else t[:89].rstrip() + "…"


def chart_caption(sample_label: str, std_name: str, verb: str) -> str:
    return f"{_clip(sample_label or 'The sample', 40)} {verb} {_clip(std_name, 48)}"


def subtitle_text(std_name: str) -> str:
    return f"GC chromatogram compared with {_clip(std_name, STD_NAME_CHARS)}"


def display_lab_id(lab_id: str) -> str:
    return _clip(lab_id or "", LAB_ID_CHARS)


# ── HTML ──────────────────────────────────────────────────────────────────
def _css(fs: float) -> str:
    f = fonts()
    lh = f"{LINE:g}"
    return f"""
@page {{ size: letter; margin: {MARGIN_TOP:g}pt {MARGIN_X:g}pt {MARGIN_BOTTOM:g}pt {MARGIN_X:g}pt; }}
body {{ font-family: {f['family']}; font-size: {fs:g}pt; line-height: {lh}; color: {INK}; background: #ffffff; margin: 0; padding: 0; }}
td {{ vertical-align: top; padding: 0; }}
p {{ margin: 0; }}
.title {{ font-size: 15pt; font-weight: bold; line-height: 1.25; color: {INK}; }}
.sub {{ font-size: 8pt; color: {MUTED}; padding-top: 2pt; }}
.cap {{ font-size: 7pt; color: {MUTED}; }}
.labid {{ font-size: 13pt; font-weight: bold; color: {INK}; line-height: 1.25; }}
.rule td {{ border-bottom: 1.25pt solid {INK}; font-size: 1pt; line-height: 1; }}
.chead td {{ padding: 10pt 0 3pt 0; }}
.ctitle {{ font-weight: bold; color: {INK}; }}
.ccap {{ color: {MUTED}; }}
.key {{ font-size: {KEY_FS:g}pt; color: {MUTED}; text-align: right; }}
.sec td.lbl {{ width: {LABEL_W:g}pt; font-weight: bold; color: {INK}; padding: 4pt 8pt 2pt 0; border-top: 0.75pt solid {BORDER}; }}
.sec td.txt {{ padding: 4pt 0 2pt 0; border-top: 0.75pt solid {BORDER}; }}
.sec td.cont {{ border-top: 0.5pt solid {HAIRLINE}; padding-top: 2pt; }}
.sec td.blank {{ border-top: 0; }}
.f-head {{ font-weight: bold; }}
.tag {{ font-size: 7pt; font-weight: bold; text-align: right; }}
.muted {{ color: {MUTED}; }}
.notes-h {{ font-weight: bold; color: {MUTED_SUNKEN}; padding-bottom: 1pt; }}
.note {{ color: {INK}; padding-bottom: 1.5pt; }}
.foot td {{ font-size: {FOOT_FS:g}pt; color: {MUTED}; line-height: 1.35; padding: 1pt 0 1pt 0; }}
.foot td.lbl {{ width: {LABEL_W:g}pt; color: {MUTED_SUNKEN}; font-weight: bold; padding-right: 8pt; }}
.foot tr.first td {{ border-top: 0.75pt solid {BORDER}; padding-top: 5pt; }}
"""


def _tag(sev: str | None) -> str:
    """A finding's severity, as the app's severity pill:
    significant white on ink, moderate ink on grey, marginal muted text."""
    if not sev:
        return ""
    if sev == "significant":
        style = f"background-color:{INK}; color:#ffffff;"
    elif sev == "moderate":
        style = f"background-color:#e2e5e9; color:{INK};"
    else:
        style = f"color:{MUTED};"
    return (f'<p class="tag"><span style="{style}">&nbsp;{_esc(sev)}&nbsp;</span></p>')


def _section(label: str, rows: list[list[tuple]]) -> str:
    """A section (table rows): its title in the left column on the first row, hairline
    rows of content beside it. *rows*: per row, ``(inner_html, style,
    colspan)`` cells (inner_html already escaped)."""
    out = []
    for i, cells in enumerate(rows):
        cls = "txt" if i == 0 else "txt cont"
        tds = []
        for inner, style, span in cells:
            attrs = f' colspan="{span}"' if span > 1 else ""
            attrs += f' style="{style}"' if style else ""
            tds.append(f'<td class="{cls}"{attrs}>{inner}</td>')
        lbl = (f'<td class="lbl">{_esc(label)}</td>' if i == 0
               else '<td class="lbl blank"></td>')
        out.append(f"<tr>{lbl}{''.join(tds)}</tr>")
    return "".join(out)


def findings_section(bullets_text: str, *, shown: int | None, no_deviation: str,
                     fs: float = TYPE_STEPS[0]) -> str:
    rows = finding_rows(bullets_text)
    if not rows:
        return _section("Findings", [[(f'<span class="muted">{_esc(no_deviation)}</span>', "", 2)]])
    total = len(rows)
    if shown is not None:
        rows = rows[:shown]
    cells = []
    for r in rows:
        head = f'<span class="f-head">{_esc(r["head"])}</span> ' if r["head"] else ""
        cells.append([(f'{head}{_esc(breakable(r["rest"], FINDING_W, fs))}', f"width:{FINDING_W:g}pt;", 1),
                      (_tag(r["severity"]), f"width:{TAG_W + 6:g}pt;", 1)])
    if len(rows) < total:
        cells.append([(f'<span class="muted">{_esc(more_findings_text(total - len(rows)))}</span>',
                       "", 2)])
    return _section("Findings", cells)


def _listed(lines: list[str], shown: int | None, more, width: float, fs: float,
            style: str = "") -> list[str]:
    """Escaped lines (as many as *shown*), then the "N more" line."""
    total = len(lines)
    view = lines if shown is None else lines[:shown]
    out = [_esc(breakable(x, width, fs)) for x in view]
    if len(view) < total:
        out.append(f'<span class="muted">{_esc(more(total - len(view), len(view)))}</span>')
    return out


def regions_section(lines: list[str], *, shown: int | None, fs: float = TYPE_STEPS[0]) -> str:
    """Marked regions: the sample's annotation comments, one line each as the
    app formats them (``text (Cx–Cy, a–b min; initials, date)``), escaped,
    in their own section between the findings and the conclusion; nothing
    when there are none."""
    if not lines:
        return ""
    rows = [[(x, "", 2)] for x in _listed(lines, shown, more_regions_text, TEXT_W, fs)]
    return "<!-- MARKED REGIONS -->" + _section("Marked regions", rows)


def conclusion_section(conclusion: str, *, chars: int | None, fs: float = TYPE_STEPS[0],
                       notes: list[str] = (), notes_shown: int | None = None,
                       note_fs: float | None = None) -> str:
    """The conclusion, then its notes: the sample's earlier comments
    (``text (initials, date)``), under a small "Notes" heading in the same
    block, a step smaller. The conclusion itself is never cut up to
    ``CONCLUSION_KEEP``."""
    if conclusion:
        body = _esc(breakable(_shorten(conclusion, chars, CONCLUSION_TAIL), TEXT_W, fs)
                    ).replace("\n", "<br>")
    else:
        body = '<span class="muted">No conclusion available.</span>'
    rows = [[(body, "", 2)]]
    if notes:
        nfs = note_fs or round(fs * 0.9, 2)
        items = "".join(f'<p class="note" style="font-size:{nfs:g}pt;">{x}</p>'
                        for x in _listed(list(notes), notes_shown, more_notes_text, TEXT_W, nfs))
        rows.append([(f'<p class="notes-h" style="font-size:{nfs:g}pt;">Notes</p>{items}', "", 2)])
    return "<!-- CONCLUSION -->" + _section("Conclusion", rows)


def _footer(footer_lines: list[str], generated: str, *, plan_: "Plan") -> str:
    rows = []
    for line in list(footer_lines) + [generated]:
        head, rest = _footer_parts(line)
        if head == "Ranges":
            rest = ranges_text(rest, plan_)
        rows.append((head, rest))
    out = []
    for i, (head, rest) in enumerate(rows):
        cls = ' class="first"' if i == 0 else ""
        out.append(f'<tr{cls}><td class="lbl">{_esc(head)}</td><td>{_esc(breakable(rest, TEXT_W, FOOT_FS))}</td></tr>')
    return f'<table class="foot" width="100%">{"".join(out)}</table>'


def _swatch(kind: str) -> str:
    import base64
    b64 = base64.b64encode(swatch_png(kind)).decode("ascii")
    return (f'<img src="data:image/png;base64,{b64}" width="{round(12 * IMG_PX_PER_PT)}" '
            f'height="{round(7 * IMG_PX_PER_PT)}" style="vertical-align:middle;">')


KEY_FS = 7.0                              # the legends' type (``.key``)
KEY_SLACK = 6.0                           # kept free in the legend's column
SPIKE_GLYPH = (f'<font color="{DEV_ABOVE}">&#9650;</font>'
               f'<font color="{DEV_BELOW}">&#9660;</font>')


def key_width(items: list[tuple[str, str, str]]) -> float:
    """A legend's width in the face in use: each item a 12pt swatch (or
    its glyphs) and a no-break space before its label, three no-break
    spaces between items."""
    sp = line_width("\u00a0", KEY_FS)
    w = 0.0
    for g, _c, t in items:
        mark = (12.0 if g == "swatch"
                else line_width(html.unescape(re.sub(r"<[^>]+>", "", g)), KEY_FS))
        w += mark + sp + line_width(t, KEY_FS)
    return w + 3 * sp * (len(items) - 1)


def chart_keys(spikes_marked: bool) -> tuple[list, list]:
    """The two legends' items, each on one line in the face in use (the
    plan counts one line): the difference key says "higher than the
    standard" where that fits its column, else "higher" / "lower" (the
    chart's caption already says "minus <standard>")."""
    trend = [("swatch", "standard", "standard"), ("swatch", "sample", "sample"),
             ("swatch", "range", "range")]
    spike = [(SPIKE_GLYPH, INK, "counted spike")] if spikes_marked else []
    diff = [("swatch", "above", "higher than the standard"), ("swatch", "below", "lower")] + spike
    if key_width(diff) > KEY_W - KEY_SLACK:
        diff = [("swatch", "above", "higher"), ("swatch", "below", "lower")] + spike
    return trend, diff


def _key(items: list[tuple[str, str, str]]) -> str:
    """A chart's legend: ``(glyph, colour, label)``, or ``("swatch", kind,
    label)`` for a drawn swatch (``swatch_png``: colour and texture)."""
    return "&nbsp;&nbsp;&nbsp;".join(
        (_swatch(c) if g == "swatch" else f'<font color="{c}">{g}</font>') + f"&nbsp;{_esc(t)}"
        for g, c, t in items)


def render_html(*, doc_name: str, lab_id: str, std_name: str, date_display: str,
                datetime_str: str, img1_b64: str, img2_b64: str, logo_b64: str,
                bullets_text: str, conclusion: str, footer_lines: list[str],
                app_version: str, plan_: Plan, region_lines: list[str] = (),
                note_lines: list[str] = (),
                sample_label: str = "", spikes_marked: bool = False,
                no_deviation: str = NO_DEVIATION_FALLBACK) -> str:
    """The report page for *plan_* (from ``plan`` with the same content)."""
    p = plan_
    e = _esc
    logo = (f'<td style="width:64pt; padding-right:10pt; vertical-align:middle;">'
            f'<img src="data:image/png;base64,{logo_b64}" height="44"></td>' if logo_b64 else "")
    lab = (f'<p class="cap">Lab ID</p><p class="labid">{e(breakable(display_lab_id(lab_id), 176, 13, True))}</p>'
           if lab_id else "")
    header = f"""
<table width="100%"><tr>
  {logo}
  <td style="vertical-align:bottom; padding-right:12pt;">
    <p class="title">{e(breakable(display_title(doc_name), TITLE_W - (LOGO_W if logo_b64 else 0), 15, True))}</p>
    <p class="sub">{e(breakable(subtitle_text(std_name), TITLE_W - (LOGO_W if logo_b64 else 0), 8))}</p>
  </td>
  <td style="width:180pt; text-align:right; vertical-align:bottom;">
    {lab}<p class="cap" style="padding-top:2pt;">{e(date_display)}</p>
  </td>
</tr></table>
<table width="100%" class="rule" style="margin-top:9pt;"><tr><td>&nbsp;</td></tr></table>"""

    w_px = round(BODY_W * IMG_PX_PER_PT)
    trend_items, diff_items = chart_keys(spikes_marked)
    trend_key = _key(trend_items)

    def chart(title: str, caption: str, key: str, b64: str, h_pt: float) -> str:
        return f"""
<table width="100%" class="chead"><tr>
  <td><span class="ctitle">{e(title)}</span>&nbsp;&nbsp;<span class="ccap">{e(caption)}</span></td>
  <td class="key" style="width:{KEY_W:g}pt; vertical-align:bottom;">{key}</td>
</tr></table>
<img src="data:image/png;base64,{b64}" width="{w_px}" height="{round(h_pt * IMG_PX_PER_PT)}">"""

    charts = (chart("Trend", chart_caption(sample_label, std_name, "against"), trend_key, img1_b64, p.trend_h)
              + chart("Difference", chart_caption(sample_label, std_name, "minus"), _key(diff_items),
                      img2_b64, p.diff_h))
    sections = ("<!-- DEVIATION ANALYSIS -->"
                + findings_section(bullets_text, shown=p.findings_shown,
                                   no_deviation=no_deviation, fs=p.fs)
                + regions_section(list(region_lines), shown=p.regions_shown, fs=p.fs)
                + conclusion_section(conclusion, chars=p.conclusion_chars, fs=p.fs,
                                     notes=list(note_lines), notes_shown=p.notes_shown,
                                     note_fs=p.note_fs))
    generated = f"Generated: GC hub {app_version}, {datetime_str}"
    footer = _footer(footer_lines, generated, plan_=p)
    return f"""<!DOCTYPE HTML>
<html><head><meta charset="utf-8"><title>{e(display_title(doc_name))}</title>
<style>{_css(p.fs)}</style></head>
<body>
<div style="-pdf-keep-in-frame-mode: shrink;">
{header}
{charts}
<table class="sec" width="100%" style="margin-top:10pt;">{sections}</table>
<!-- FOOTER -->
<table width="100%" style="margin-top:10pt;"><tr><td>{footer}</td></tr></table>
</div>
</body></html>"""


def page_count(pdf: bytes) -> int:
    import io

    import pypdf
    return len(pypdf.PdfReader(io.BytesIO(pdf)).pages)
