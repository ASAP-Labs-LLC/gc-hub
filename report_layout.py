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
3. still too long: shortens the footer's ranges line (the ranges are also
   on the charts and in the findings) to ``RANGES_CHARS``, "…, and N more";
4. then lists as many comments as fit (in their order) and one line
   "N more comments on this sample are in the GC hub";
5. then lists as many findings as fit (at least one) and "N more findings
   are in the GC hub" (and gives the comments back what that freed);
6. a conclusion is printed whole up to ``CONCLUSION_KEEP`` characters (the
   cap the hub enforces); only a longer one (an old queue item, an API
   client) is shortened, never below the cap, at a word, ending
   "… (shortened; the full conclusion is in the GC hub)".

With the conclusion at its cap, every section at its worst and the
smallest type, one finding and the footer still fit beside the smallest
charts (``tests/test_report_one_page.py``).

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
LABEL_W = 84.0                            # the section-title column
KEY_W = 200.0                             # a chart's legend
TITLE_W = BODY_W - 192.0                  # the title column (the lab ID takes 180 + gap)
LOGO_W = 74.0                             # a logo and its gap, when there is one
TAG_W = 56.0                              # a finding's severity tag
TEXT_W = BODY_W - LABEL_W                 # a section's text column
FINDING_W = TEXT_W - TAG_W - 6.0
IMG_PX_PER_PT = 1 / 0.75                  # xhtml2pdf reads an <img> width in CSS px

CHART_MAX = (284.0, 178.0)                # trend, difference
CHART_MIN = (150.0, 96.0)
TYPE_STEPS = (8.5, 7.8, 7.2, 6.8)         # body type, largest first
FOOT_FS = 6.5
LINE = 1.38                               # line height, in ems
SLACK = 18.0                              # points kept back from the measurement
RANGES_CHARS = 300                        # the footer's ranges line, when shortened
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


def line_count(text: str, width: float, size: float, bold: bool = False) -> int:
    """Lines *text* takes in a column *width* points wide (word wrap; a word
    wider than the column breaks, as xhtml2pdf breaks it)."""
    f = fonts()
    font = f["bold"] if bold else f["regular"]
    total = 0
    for para in str(text).split("\n"):
        words = para.split()
        if not words:
            total += 1
            continue
        lines, cur = 1, 0.0
        space = _width(" ", font, size)
        for w in words:
            ww = _width(w, font, size)
            if ww > width:                         # a long token wraps on its own
                extra = math.ceil(ww / width)
                lines += extra if cur else extra - 1
                cur = ww - (extra - 1) * width
                continue
            need = ww if cur == 0 else cur + space + ww
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
    was shortened (``None`` = nothing)."""
    trend_h: float
    diff_h: float
    fs: float
    findings_shown: int | None = None      # None: all
    comments_shown: int | None = None
    conclusion_chars: int | None = None
    ranges_chars: int | None = None
    notes: list = field(default_factory=list)

    @property
    def width(self) -> float:
        return BODY_W


def _shorten(text: str, limit: int, tail: str) -> str:
    if limit is None or len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.—–-")
    return f"{cut}… {tail}"


CONCLUSION_TAIL = "(shortened; the full conclusion is in the GC hub)"


def _more(n: int, one: str, many: str) -> str:
    return (one if n == 1 else many).format(n=n)


def more_comments_text(n: int, shown: int = 1) -> str:
    if not shown:
        return _more(n, "1 comment on this sample is in the GC hub.",
                     "{n} comments on this sample are in the GC hub.")
    return _more(n, "1 more comment on this sample is in the GC hub.",
                 "{n} more comments on this sample are in the GC hub.")


def more_findings_text(n: int) -> str:
    return _more(n, "1 more finding is in the GC hub.", "{n} more findings are in the GC hub.")


def _footer_parts(line: str) -> tuple[str, str]:
    head, sep, rest = line.partition(": ")
    return (head, rest) if sep and len(head) <= 14 else ("", line)


def _ranges_short(line: str, limit: int | None) -> str:
    if limit is None or len(line) <= limit:
        return line
    shown = line[:limit].rsplit(", ", 1)[0]
    left = line[len(shown):].count(", ")
    return f"{shown}, and {left} more" if left else shown


def _text_height(plan: Plan, *, title: str, subtitle: str, lab_id: str, std_name: str,
                 rows: list[dict], conclusion: str, comment_lines: list[str],
                 footer_lines: list[str], has_logo: bool = False) -> float:
    fs = plan.fs
    h = 0.0
    title_w = TITLE_W - (LOGO_W if has_logo else 0)
    # header: title + subtitle at left (wraps), lab ID + date at right
    left = _block_h(line_count(title, title_w, 15, True), 15) + \
        _block_h(line_count(subtitle, title_w, 8), 8)
    right = 9 + _block_h(line_count(lab_id or "", 180, 13, True), 13) + 12
    h += max(left, right, 42) + 12 + 1.5
    # chart chrome (title rows and gaps), both charts; a long caption wraps
    for verb in ("against", "minus"):
        cap = "Difference  " + chart_caption(lab_id, std_name, verb)
        h += _block_h(line_count(cap, BODY_W - KEY_W, fs), fs) + 26
    # findings
    shown = rows if plan.findings_shown is None else rows[:plan.findings_shown]
    fh = 0.0
    for r in shown or [{"text": NO_DEVIATION_FALLBACK}]:
        fh += _block_h(line_count(r["text"], FINDING_W, fs), fs) + 6
    if plan.findings_shown is not None and plan.findings_shown < len(rows):
        fh += _block_h(1, fs) + 6
    h += fh + 8
    # conclusion
    concl = _shorten(conclusion, plan.conclusion_chars, CONCLUSION_TAIL)
    h += _block_h(line_count(concl or "No conclusion available.", TEXT_W, fs), fs) + 14
    # comments
    if comment_lines:
        shown_c = comment_lines if plan.comments_shown is None else comment_lines[:plan.comments_shown]
        ch = sum(_block_h(line_count(c, TEXT_W, fs), fs) + 4 for c in shown_c)
        if len(shown_c) < len(comment_lines):
            ch += _block_h(1, fs) + 4
        h += ch + 10
    # footer
    foot = 0.0
    for line in footer_lines:
        head, rest = _footer_parts(line)
        if head == "Ranges":
            rest = _ranges_short(rest, plan.ranges_chars)
        foot += _block_h(line_count(rest, TEXT_W, FOOT_FS), FOOT_FS) + 2
    foot += _block_h(1, FOOT_FS) + 2
    h += foot + 14
    return h * 1.03 + SLACK


def plan(*, doc_name: str, lab_id: str, std_name: str, bullets_text: str,
         conclusion: str, comment_lines: list[str], footer_lines: list[str],
         has_logo: bool = False) -> Plan:
    """The one-page plan for this content (see the module docstring)."""
    title = display_title(doc_name)
    subtitle = subtitle_text(std_name)
    rows = finding_rows(bullets_text)
    comment_lines = list(comment_lines or [])
    kw = dict(title=title, subtitle=subtitle, lab_id=lab_id, std_name=std_name, rows=rows,
              conclusion=conclusion or "", comment_lines=comment_lines,
              footer_lines=list(footer_lines or []), has_logo=has_logo)
    ratio = CHART_MAX[1] / CHART_MAX[0]
    min_charts = CHART_MIN[0] + CHART_MIN[1]

    def charts_for(p: Plan) -> float:
        return BODY_H - _text_height(p, **kw)

    p = Plan(trend_h=CHART_MIN[0], diff_h=CHART_MIN[1], fs=TYPE_STEPS[0])
    for fs in TYPE_STEPS:                         # 1-2: the type steps down
        p.fs = fs
        if charts_for(p) >= min_charts:
            break
    ranges_len = max((len(_footer_parts(x)[1]) for x in kw["footer_lines"]
                      if _footer_parts(x)[0] == "Ranges"), default=0)
    if charts_for(p) < min_charts and ranges_len > RANGES_CHARS:   # 3: the ranges line
        p.ranges_chars = RANGES_CHARS
        p.notes.append("ranges line shortened")
    if charts_for(p) < min_charts and comment_lines:       # 4: comments
        p.comments_shown = len(comment_lines)
        while p.comments_shown > 0 and charts_for(p) < min_charts:
            p.comments_shown -= 1
        p.notes.append(f"comments: {p.comments_shown} of {len(comment_lines)} listed")
    if charts_for(p) < min_charts and len(rows) > 1:       # 5: findings
        p.findings_shown = len(rows)
        while p.findings_shown > 1 and charts_for(p) < min_charts:
            p.findings_shown -= 1
        p.notes.append(f"findings: {p.findings_shown} of {len(rows)} listed")
    if charts_for(p) < min_charts and len(conclusion or "") > CONCLUSION_KEEP:   # 6
        lo, hi = CONCLUSION_KEEP, len(conclusion)
        while lo < hi:                                     # the longest that fits
            mid = (lo + hi + 1) // 2
            p.conclusion_chars = mid
            if charts_for(p) >= min_charts:
                lo = mid
            else:
                hi = mid - 1
        p.conclusion_chars = lo
        p.notes.append(f"conclusion shortened to {lo} of {len(conclusion)} characters")
    if p.comments_shown is not None and p.findings_shown is not None:
        # the findings step freed room: give comments back what now fits
        n = p.comments_shown
        while n < len(comment_lines):
            p.comments_shown = n + 1
            if charts_for(p) < min_charts:
                p.comments_shown = n
                break
            n += 1
        p.notes = [x for x in p.notes if not x.startswith("comments:")]
        if p.comments_shown < len(comment_lines):
            p.notes.append(f"comments: {p.comments_shown} of {len(comment_lines)} listed")
        else:
            p.comments_shown = None
    room = max(charts_for(p), min_charts)
    trend = min(CHART_MAX[0], max(CHART_MIN[0], room / (1 + ratio)))
    diff = min(CHART_MAX[1], max(CHART_MIN[1], room - trend))
    p.trend_h, p.diff_h = round(trend), round(diff)
    return p


def display_title(doc_name: str) -> str:
    t = " ".join(str(doc_name or "GC Analysis").split()) or "GC Analysis"
    return t if len(t) <= 90 else t[:89].rstrip() + "…"


def chart_caption(sample_label: str, std_name: str, verb: str) -> str:
    return f"{_clip(sample_label or 'The sample', 40)} {verb} {_clip(std_name, 48)}"


def subtitle_text(std_name: str) -> str:
    return f"GC chromatogram compared with {std_name}"


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
.key {{ font-size: 7pt; color: {MUTED}; text-align: right; }}
.sec td.lbl {{ width: {LABEL_W:g}pt; font-weight: bold; color: {INK}; padding: 4pt 8pt 2pt 0; border-top: 0.75pt solid {BORDER}; }}
.sec td.txt {{ padding: 4pt 0 2pt 0; border-top: 0.75pt solid {BORDER}; }}
.sec td.cont {{ border-top: 0.5pt solid {HAIRLINE}; padding-top: 2pt; }}
.sec td.blank {{ border-top: 0; }}
.f-head {{ font-weight: bold; }}
.tag {{ font-size: 7pt; font-weight: bold; text-align: right; }}
.muted {{ color: {MUTED}; }}
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


def conclusion_section(conclusion: str, *, chars: int | None, fs: float = TYPE_STEPS[0]) -> str:
    if conclusion:
        body = _esc(breakable(_shorten(conclusion, chars, CONCLUSION_TAIL), TEXT_W, fs)
                    ).replace("\n", "<br>")
    else:
        body = '<span class="muted">No conclusion available.</span>'
    return _section("Conclusion", [[(body, "", 2)]])


def comments_section(lines: list[str], *, shown: int | None, fs: float = TYPE_STEPS[0]) -> str:
    """The report's comments (one line each, already formatted by the app:
    ``text (initials, date)`` / ``text (Cx–Cy, a–b min; initials, date)``),
    escaped; nothing when there are none. Kept on its own so what the
    section lists can change without touching the rest of the page."""
    if not lines:
        return ""
    total = len(lines)
    lines = lines if shown is None else lines[:shown]
    cells = [[(_esc(breakable(c, TEXT_W, fs)), "", 2)] for c in lines]
    if len(lines) < total:
        cells.append([(f'<span class="muted">{_esc(more_comments_text(total - len(lines), len(lines)))}</span>',
                       "", 2)])
    return _section("Comments", cells)


def _footer(footer_lines: list[str], generated: str, *, ranges_chars: int | None) -> str:
    rows = []
    for line in list(footer_lines) + [generated]:
        head, rest = _footer_parts(line)
        if head == "Ranges":
            rest = _ranges_short(rest, ranges_chars)
        rows.append((head, rest))
    out = []
    for i, (head, rest) in enumerate(rows):
        cls = ' class="first"' if i == 0 else ""
        out.append(f'<tr{cls}><td class="lbl">{_esc(head)}</td><td>{_esc(breakable(rest, TEXT_W, FOOT_FS))}</td></tr>')
    return f'<table class="foot" width="100%">{"".join(out)}</table>'


def _key(items: list[tuple[str, str, str]]) -> str:
    """A chart's legend: ``(glyph, colour, label)``."""
    return "&nbsp;&nbsp;&nbsp;".join(
        f'<font color="{c}">{g}</font>&nbsp;{_esc(t)}' for g, c, t in items)


def render_html(*, doc_name: str, lab_id: str, std_name: str, date_display: str,
                datetime_str: str, img1_b64: str, img2_b64: str, logo_b64: str,
                bullets_text: str, conclusion: str, comment_lines: list[str],
                footer_lines: list[str], app_version: str, plan_: Plan,
                sample_label: str = "", spikes_marked: bool = False,
                no_deviation: str = NO_DEVIATION_FALLBACK) -> str:
    """The report page for *plan_* (from ``plan`` with the same content)."""
    p = plan_
    e = _esc
    logo = (f'<td style="width:64pt; padding-right:10pt; vertical-align:middle;">'
            f'<img src="data:image/png;base64,{logo_b64}" height="44"></td>' if logo_b64 else "")
    lab = (f'<p class="cap">Lab ID</p><p class="labid">{e(breakable(lab_id, 176, 13, True))}</p>'
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
    trend_key = _key([("&#9632;", CHART_REF, "standard"), ("&#8212;", INK, "sample"),
                      ("&#9632;", BORDER, "range")])
    diff_items = [("&#9632;", DEV_ABOVE_FILL, "higher than the standard"),
                  ("&#9632;", DEV_BELOW_FILL, "lower")]
    if spikes_marked:
        diff_items.append(("&#9650;", INK, "counted spike"))

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
    sections = (findings_section(bullets_text, shown=p.findings_shown,
                                 no_deviation=no_deviation, fs=p.fs)
                + conclusion_section(conclusion, chars=p.conclusion_chars, fs=p.fs)
                + comments_section(comment_lines, shown=p.comments_shown, fs=p.fs))
    generated = f"Generated: GC hub {app_version}, {datetime_str}"
    footer = _footer(footer_lines, generated, ranges_chars=p.ranges_chars)
    return f"""<!DOCTYPE HTML>
<html><head><meta charset="utf-8"><title>{e(display_title(doc_name))}</title>
<style>{_css(p.fs)}</style></head>
<body>
<div style="-pdf-keep-in-frame-mode: shrink;">
{header}
{charts}
<table class="sec" width="100%" style="margin-top:10pt;">{sections}</table>
<table width="100%" style="margin-top:10pt;"><tr><td>{footer}</td></tr></table>
</div>
</body></html>"""


def page_count(pdf: bytes) -> int:
    import io

    import pypdf
    return len(pypdf.PdfReader(io.BytesIO(pdf)).pages)
