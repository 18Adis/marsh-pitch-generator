"""
Stage 5b — Pitch deck renderer (template).

Renders a generated PitchContent (+ the CompanyProfile, for sourced facts)
into a designed 16:9 deck. Content is NOT changed here — this module only
decides layout, typography and colour. Design rules follow the project's
PPTX skill: one dominant colour (navy) with a teal accent, a serif header /
sans body pairing (Cambria / Calibri — both ship with Office and render
true-to-width in QA), stat callouts, numbered circles as the one repeated
motif, dark "sandwich" slide for the recommendation, no accent stripes or
title underlines, 0.5" margins, and text sized to fit its box.

Slides (always 3-5, per the brief):
  1. Company overview    — navy panel + sourced stat cards + risks (labelled assumptions)
  2. Why choose Marsh    — lead statement + numbered cards
  3. Benefits mapped     — styled table: exposure | benefit | source (+ continuation if > 6 rows)
  4. Recommendation      — dark slide, talking-point cards, comparative rationale
  5. Sources & assumptions — company web sources + everything to confirm (dropped only
                              if a benefits continuation slide would exceed 5 slides)

Built with python-pptx (not pptxgenjs) so the Streamlit app needs no Node
runtime. Speaker notes carry the exact source quotes for every benefit.
"""

from __future__ import annotations

import math
import re
from datetime import date
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

# ------------------------------------------------------------
# Palette & type
# ------------------------------------------------------------
NAVY = RGBColor(0x0B, 0x25, 0x45)
TEAL = RGBColor(0x13, 0x80, 0x7A)
MINT = RGBColor(0x7F, 0xD1, 0xC7)       # accent text on navy
SOFT = RGBColor(0xF4, 0xF7, 0xFA)       # card background on white
INK = RGBColor(0x1F, 0x29, 0x33)
MUTE = RGBColor(0x5F, 0x6B, 0x7A)
PALE = RGBColor(0xD6, 0xE2, 0xEE)       # body text on navy
FOOT_ON_NAVY = RGBColor(0xA9, 0xB8, 0xC9)
LINE = RGBColor(0xD5, 0xDD, 0xE5)
CORAL = RGBColor(0xE4, 0x57, 0x2E)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

HEAD = "Cambria"
BODY = "Calibri"

SLIDE_W, SLIDE_H = 10.0, 5.625
MARGIN = 0.5
MAX_BENEFIT_ROWS = 6


# ------------------------------------------------------------
# Text fitting (heuristic — python-pptx cannot measure text)
# ------------------------------------------------------------

def _lines_needed(text: str, width_in: float, size_pt: float, char_w: float = 0.5) -> int:
    chars_per_line = max(1, int(width_in * 72 / (size_pt * char_w)))
    total = 0
    for para in (text or "").split("\n"):
        words, line_len, lines = para.split(), 0, 1
        for w in words:
            add = len(w) + (1 if line_len else 0)
            if line_len + add > chars_per_line:
                lines += 1 + (len(w) // chars_per_line)
                line_len = len(w) % chars_per_line
            else:
                line_len += add
        total += lines
    return total


def fit_size(text: str, width_in: float, height_in: float, sizes=(16, 14, 13, 12, 11, 10.5, 10, 9.5, 9, 8.5, 8),
             spacing: float = 1.18, char_w: float = 0.5) -> float:
    """Largest size in `sizes` whose estimated wrapped height fits the box."""
    for s in sizes:
        if _lines_needed(text, width_in, s, char_w) * s * spacing / 72 <= height_in:
            return s
    return sizes[-1]


def common_size(texts, width_in: float, height_in: float, sizes) -> float:
    """One size for a group of boxes (rows/cards) so they look consistent."""
    return min((fit_size(t, width_in, height_in, sizes) for t in texts), default=sizes[0])


def text_height(text: str, width_in: float, size_pt: float, spacing: float = 1.18) -> float:
    return _lines_needed(text, width_in, size_pt) * size_pt * spacing / 72


def shorten(text: str, width_in: float, height_in: float, size_pt: float, spacing: float = 1.18) -> str:
    """Last resort: trim at a word boundary with an ellipsis so text never overflows."""
    text = " ".join((text or "").split())
    if _lines_needed(text, width_in, size_pt) * size_pt * spacing / 72 <= height_in:
        return text
    words = text.split()
    while words and _lines_needed(" ".join(words) + "…", width_in, size_pt) * size_pt * spacing / 72 > height_in:
        words.pop()
    return (" ".join(words).rstrip(",;:—-") + "…") if words else ""


def clean(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text.replace(" ,", ",").replace(" .", ".").strip()


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9“\"(])", clean(text)) if s.strip()]


def split_title(text: str, max_title: int = 60) -> tuple[str, str]:
    """'Sedentary desk work: long hours sitting...' -> ('Sedentary desk work', 'long hours sitting...')."""
    t = clean(text)
    m = re.match(r"^(.{3,%d}?)\s*(?::|—|–| - )\s+(.+)$" % max_title, t)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return t, ""


# ------------------------------------------------------------
# Low-level drawing helpers
# ------------------------------------------------------------

class Deck:
    def __init__(self):
        self.prs = Presentation()
        self.prs.slide_width = Inches(SLIDE_W)
        self.prs.slide_height = Inches(SLIDE_H)
        self.blank = self.prs.slide_layouts[6]

    def slide(self, bg: RGBColor = WHITE):
        s = self.prs.slides.add_slide(self.blank)
        fill = s.background.fill
        fill.solid()
        fill.fore_color.rgb = bg
        return s


def rect(slide, x, y, w, h, color, shape=MSO_SHAPE.RECTANGLE):
    shp = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = color
    shp.line.fill.background()
    style = shp._element.find(qn("p:style"))
    if style is not None:          # theme style adds a drop shadow in some viewers
        shp._element.remove(style)
    return shp


def text(slide, x, y, w, h, runs, size=12, font=BODY, color=INK, bold=False, align=PP_ALIGN.LEFT,
         anchor=MSO_ANCHOR.TOP, spacing=None, char_spacing=None):
    """
    Adds a text box. `runs` is a str, or a list of paragraphs, where each
    paragraph is a str or a list of (text, {overrides}) tuples.
    """
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    paras = runs if isinstance(runs, list) else [runs]
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        if spacing:
            p.line_spacing = spacing
        parts = para if isinstance(para, list) else [(para, {})]
        for part, opts in parts:
            r = p.add_run()
            r.text = part
            f = r.font
            f.name = opts.get("font", font)
            f.size = Pt(opts.get("size", size))
            f.bold = opts.get("bold", bold)
            f.italic = opts.get("italic", False)
            f.color.rgb = opts.get("color", color)
            cs = opts.get("char_spacing", char_spacing)
            if cs:
                r._r.get_or_add_rPr().set("spc", str(int(cs * 100)))
    return tb


def circle(slide, x, y, d, label, fill=TEAL, size=12):
    c = rect(slide, x, y, d, d, fill, MSO_SHAPE.OVAL)
    tf = c.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = label
    r.font.name, r.font.size, r.font.bold, r.font.color.rgb = BODY, Pt(size), True, WHITE
    return c


def footer(slide, left: str, idx: int, total: int, dark: bool = False, left_x: float = MARGIN,
           left_w: float = 7.2):
    col = FOOT_ON_NAVY if dark else MUTE
    text(slide, left_x, 5.13, left_w, 0.36, shorten(left, left_w, 0.36, 7.5), size=7.5, color=col,
         anchor=MSO_ANCHOR.MIDDLE)
    text(slide, 7.9, 5.13, 1.6, 0.36, f"Draft for advisor review  ·  {idx}/{total}", size=7.5, color=col,
         align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)


def title(slide, t: str, sub: str | None = None):
    text(slide, MARGIN, 0.32, SLIDE_W - 2 * MARGIN, 0.55, t, size=fit_size(t, 9, 0.55, (26, 24, 22, 20)),
         font=HEAD, bold=True, color=NAVY, anchor=MSO_ANCHOR.MIDDLE)
    if sub:
        s = fit_size(sub, 9, 0.34, (12, 11, 10.5, 10))
        text(slide, MARGIN, 0.88, SLIDE_W - 2 * MARGIN, 0.34, shorten(sub, 9, 0.34, s), size=s, color=MUTE)


def notes(slide, body: str):
    slide.notes_slide.notes_text_frame.text = body


def style_table(tbl, header_fill=NAVY):
    """Remove the default banded style so our fills are the only styling."""
    tbl_pr = tbl._tbl.tblPr
    for attr in ("bandRow", "firstRow", "bandCol", "firstCol", "lastRow", "lastCol"):
        tbl_pr.set(attr, "0")
    style_id = tbl_pr.find(qn("a:tableStyleId"))
    if style_id is not None:
        style_id.text = "{5940675A-B579-460E-94D1-54222C63F5DA}"  # "No Style, Table Grid"


def cell_text(cell, value, size, bold=False, color=INK, fill=WHITE, font=BODY, align=PP_ALIGN.LEFT):
    cell.fill.solid()
    cell.fill.fore_color.rgb = fill
    cell.margin_left = cell.margin_right = Inches(0.08)
    cell.margin_top = cell.margin_bottom = Inches(0.04)
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf = cell.text_frame
    tf.word_wrap = True
    tf.paragraphs[0].text = ""
    p = tf.paragraphs[0]
    p.alignment = align
    r = p.add_run()
    r.text = value
    r.font.name, r.font.size, r.font.bold, r.font.color.rgb = font, Pt(size), bold, color


def cell_border(cell, color="D5DDE5", width_pt=0.75):
    tc_pr = cell._tc.get_or_add_tcPr()
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        ln = tc_pr.find(qn(tag))
        if ln is None:
            ln = tc_pr.makeelement(qn(tag), {})
            tc_pr.append(ln)
        ln.set("w", str(int(width_pt * 12700)))
        for child in list(ln):
            ln.remove(child)
        sf = ln.makeelement(qn("a:solidFill"), {})
        clr = sf.makeelement(qn("a:srgbClr"), {"val": color})
        sf.append(clr)
        ln.append(sf)


# ------------------------------------------------------------
# Content helpers
# ------------------------------------------------------------

_STAT = re.compile(r"([₹$€£]\s?)?\d[\d,]*(?:\.\d+)?\s?(?:%|\+|\s?(?:lakh crore|crore|cr|lakh|billion|bn|million|mn|countries|clients))?",
                   flags=re.I)


def stat_from_fact(fact) -> tuple[str, str] | None:
    """('3,28,594', 'Employees · fiscal 2026') from a sourced fact, or None if it has no figure."""
    value = clean(getattr(fact, "value", ""))
    m = _STAT.search(value)
    if not m or len(m.group(0).strip()) < 2:
        return None
    big = m.group(0).strip()
    rest = clean((value[:m.start()] + " " + value[m.end():]).strip(" ,;:-–—()"))
    label = clean(getattr(fact, "label", ""))
    dangling = {"in", "of", "at", "across", "with", "for", "to", "from", "by"}
    if rest and rest.split()[-1].lower() in dangling:
        rest = ""
    if not rest:
        small = label
    elif label.lower() in rest.lower() or rest.lower().startswith(label.lower().split()[0]):
        small = rest
    else:
        small = rest if len(rest) > 18 else f"{label} · {rest}"
    return big, small


def policy_short(name: str) -> str:
    """'HDFC ERGO's Optima Secure+' -> 'HDFC ERGO Optima Secure+' (drops possessives only)."""
    return re.sub(r"[’']s\b", "", clean(name))


def short_url(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url or "")


# ------------------------------------------------------------
# Slides
# ------------------------------------------------------------

def _slide_overview(deck: Deck, pitch, profile, idx, total, month):
    s = deck.slide()
    rect(s, 0, 0, 4.1, SLIDE_H, NAVY)
    text(s, MARGIN, 0.45, 3.3, 0.3, f"CLIENT PITCH  ·  {month.upper()}", size=10, bold=True, color=MINT, char_spacing=2)
    name = clean(pitch.company_name)
    ns = fit_size(name, 3.3, 1.0, (32, 28, 26, 24, 22, 20, 18, 16), char_w=0.55)
    name_h = max(0.55, min(1.0, text_height(name, 3.3, ns) + 0.05))
    text(s, MARGIN, 0.85, 3.3, name_h, shorten(name, 3.3, 1.0, ns), size=ns, font=HEAD, bold=True, color=WHITE)
    sub_y = 0.85 + name_h + 0.08
    text(s, MARGIN, sub_y, 3.3, 0.5, "Employee health insurance proposal, grounded in four insurer policy documents",
         size=11.5 if name_h > 0.8 else 12, color=PALE)

    live = bool(profile and getattr(profile, "data_source", "") == "live_web")
    rows = [
        ("INDUSTRY", clean(pitch.industry)),
        ("SIZE", clean(pitch.size_estimate)),
        ("COMPANY DATA", f"Live web research · {len(profile.sources)} sources" if live
         else "Model knowledge only — figures are assumptions"),
        ("RECOMMENDED PLAN", clean(pitch.recommended_policy.policy_name)),
    ]
    y = max(2.4, sub_y + 0.62)
    step = min(0.62, (5.0 - y) / len(rows))
    for k, v in rows:
        text(s, MARGIN, y, 3.3, 0.2, k, size=8.5, bold=True, color=MINT, char_spacing=1)
        vs = fit_size(v, 3.3, 0.34, (11, 10.5, 10, 9.5, 9))
        text(s, MARGIN, y + 0.2, 3.3, 0.36, shorten(v, 3.3, 0.36, vs), size=vs, color=WHITE)
        y += step

    # Right: stat cards from sourced, verified facts
    stats = []
    for f in (getattr(profile, "facts", None) or []):
        if getattr(f, "verified_in_source", False):
            st = stat_from_fact(f)
            if st:
                stats.append((st, f))
        if len(stats) == 4:
            break

    right_x, right_w = 4.5, 5.0
    if stats:
        cols = 2
        card_w, card_h = 2.4, 0.8
        for i, ((big, small), f) in enumerate(stats):
            cx = right_x + (i % cols) * (card_w + 0.2)
            cy = 0.45 + (i // cols) * (card_h + 0.12)
            rect(s, cx, cy, card_w, card_h, SOFT)
            bs = fit_size(big, card_w - 0.3, 0.4, (21, 20, 18, 16, 14), char_w=0.55)
            text(s, cx + 0.15, cy + 0.06, card_w - 0.3, 0.4, big, size=bs, font=HEAD, bold=True, color=NAVY,
                 anchor=MSO_ANCHOR.MIDDLE)
            ss = fit_size(small, card_w - 0.3, 0.28, (10, 9.5, 9, 8.5, 8))
            text(s, cx + 0.15, cy + 0.48, card_w - 0.3, 0.28, shorten(small, card_w - 0.3, 0.28, ss), size=ss, color=MUTE)
        risks_y = 0.45 + math.ceil(len(stats) / cols) * (card_h + 0.12) + 0.06
    else:
        rect(s, right_x, 0.45, right_w, 0.7, SOFT)
        text(s, right_x + 0.2, 0.45, right_w - 0.4, 0.7,
             "No verified company figures available — company details on this slide are assumptions "
             "for the advisor to confirm.", size=10.5, color=MUTE, anchor=MSO_ANCHOR.MIDDLE)
        risks_y = 1.4

    text(s, right_x, risks_y, 3.3, 0.3, "Key workforce health risks", size=14, font=HEAD, bold=True, color=NAVY,
         anchor=MSO_ANCHOR.MIDDLE)
    text(s, right_x + 3.1, risks_y, 1.9, 0.3, "ASSUMPTIONS — advisor to confirm", size=7.5, bold=True, color=CORAL,
         align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
    risks = [clean(r) for r in (pitch.key_risks_summary or [])][:5]
    avail = 5.08 - (risks_y + 0.4)
    row_h = min(0.62, avail / max(1, len(risks)))
    risk_texts = []
    for r in risks:
        head, detail = split_title(r)
        risk_texts.append((head, detail, f"{head} — {detail}" if detail else head))
    # readable floor of 9.5pt: an over-long risk is trimmed rather than shrinking every row
    size = common_size([t for _, _, t in risk_texts], 4.5, row_h - 0.04, (11, 10.5, 10, 9.5))
    for i, (head, detail, body_txt) in enumerate(risk_texts):
        ry = risks_y + 0.4 + i * row_h
        circle(s, right_x, ry + (row_h - 0.3) / 2, 0.3, str(i + 1), size=10.5)
        body_txt = shorten(body_txt, 4.5, row_h - 0.04, size)
        if detail and body_txt.startswith(head):
            runs = [[(head, {"bold": True, "color": INK}), (body_txt[len(head):], {"color": MUTE})]]
        else:
            runs = [[(body_txt, {"color": INK})]]
        text(s, right_x + 0.45, ry, 4.55, row_h, runs, size=size, anchor=MSO_ANCHOR.MIDDLE)

    src_line = ("Company figures: live web research (see Sources slide). Risks are inferred assumptions."
                if live else "No live company data — figures and risks are labelled assumptions.")
    footer(s, src_line, idx, total, left_x=4.5, left_w=3.3)

    n = [f"Company data source: {'live web research' if live else 'model knowledge (no live data)'}."]
    for f in (getattr(profile, "facts", None) or []):
        n.append(f"- {f.label}: {f.value} [{f.source_id}] {f.source_url}"
                 + ("" if f.verified_in_source else "  (NOT verified in source — do not quote)"))
    n.append("Key risks (full text — inferred, confirm with the client's HR team):")
    n += [f"{i}. {clean(r)}" for i, r in enumerate(pitch.key_risks_summary or [], 1)]
    notes(s, "\n".join(n))


def _slide_why_marsh(deck: Deck, pitch, idx, total):
    s = deck.slide()
    parts = sentences(pitch.why_marsh) or [clean(pitch.why_marsh)]
    if len(parts) >= 3:
        lead, cards = parts[0], parts[1:5]
    else:
        lead, cards = "An independent, evidence-first way to buy health cover for your workforce", parts[:4]
    title(s, "Why choose Marsh", lead)

    n = len(cards)
    cols = 2 if n in (2, 4) else (1 if n == 1 else 3)
    rows = math.ceil(n / cols)
    gap = 0.25
    area_y, area_h = 1.45, 3.45
    card_w = (SLIDE_W - 2 * MARGIN - (cols - 1) * gap) / cols
    max_h = (area_h - (rows - 1) * gap) / rows
    tw = card_w - 1.05
    size = common_size(cards, tw, max_h - 0.45, (16, 15, 14, 13, 12, 11.5, 11, 10.5, 10))
    need = max((text_height(c, tw, size) for c in cards), default=0.5)
    card_h = min(max_h, max(1.7, need + 0.55))
    block_h = rows * card_h + (rows - 1) * gap
    top = area_y + max(0.0, (area_h - block_h) / 3)
    for i, c in enumerate(cards):
        cx = MARGIN + (i % cols) * (card_w + gap)
        cy = top + (i // cols) * (card_h + gap)
        rect(s, cx, cy, card_w, card_h, SOFT)
        circle(s, cx + 0.25, cy + 0.25, 0.46, str(i + 1), fill=TEAL if i % 2 == 0 else NAVY)
        th = card_h - 0.45
        text(s, cx + 0.9, cy + 0.25, tw, th, shorten(c, tw, th, size), size=size, color=INK)
    footer(s, "Marsh service statements are outside the policy documents — confirm scope before sending.", idx, total)
    notes(s, "Why-Marsh statements are advisory/service claims, not policy terms. They are not traceable to the "
             "four policy brochures and must be confirmed by the advisor against Marsh's service scope.")


def _slide_benefits(deck: Deck, pitch, rows, idx, total, cont: bool):
    s = deck.slide()
    title(s, "Policy benefits mapped to your risks" + (" (continued)" if cont else ""),
          "Each benefit is stated as written in the insurer's brochure, with its source page")
    if not rows:
        rect(s, MARGIN, 1.5, 9, 1.2, SOFT)
        text(s, MARGIN + 0.3, 1.5, 8.4, 1.2,
             "No policy benefits could be confidently grounded in the source documents for the identified risks. "
             "The advisor should review the risks with the client before proposing cover.",
             size=13, color=INK, anchor=MSO_ANCHOR.MIDDLE)
        footer(s, "Source: the four insurer policy brochures supplied for this proposal.", idx, total)
        return

    col_w = [2.3, 4.9, 1.8]
    avail_h = 5.0 - 1.35 - 0.34
    pad = 0.14

    def row_need(b, size):
        head, _ = split_title(b.risk, 48)
        return max(text_height(head, col_w[0] - 0.2, size), text_height(clean(b.matched_benefit), col_w[1] - 0.2, size),
                   text_height(policy_short(b.source_policy) + " p.0", col_w[2] - 0.2, size - 0.5) + size * 1.2 / 72,
                   0.3) + pad

    body = 9
    for cand in (11, 10.5, 10, 9.5, 9):
        if sum(row_need(b, cand) for b in rows) <= avail_h:
            body = cand
            break
    heights = [row_need(b, body) for b in rows]
    over = sum(heights) - avail_h
    if over > 0:  # even at 9pt: scale rows down and trim text to fit (rare)
        heights = [h * avail_h / sum(heights) for h in heights]

    shp = s.shapes.add_table(len(rows) + 1, 3, Inches(MARGIN), Inches(1.35), Inches(9), Inches(0.34 + sum(heights)))
    tbl = shp.table
    style_table(tbl)
    for i, w in enumerate(col_w):
        tbl.columns[i].width = Inches(w)
    tbl.rows[0].height = Inches(0.34)
    for i, h in enumerate(("Your risk", "Policy benefit", "Source")):
        cell_text(tbl.cell(0, i), h, 10, bold=True, color=WHITE, fill=NAVY)
        cell_border(tbl.cell(0, i))
    for r, (b, rh) in enumerate(zip(rows, heights), 1):
        tbl.rows[r].height = Inches(rh)
        head, _ = split_title(b.risk, 48)
        cell_text(tbl.cell(r, 0), shorten(head, col_w[0] - 0.2, rh - pad, body), body, bold=True, color=NAVY)
        cell_text(tbl.cell(r, 1), shorten(clean(b.matched_benefit), col_w[1] - 0.2, rh - pad, body), body)
        cell_text(tbl.cell(r, 2), f"{policy_short(b.source_policy)}\np.{b.page_number}", body - 0.5, color=MUTE,
                  fill=SOFT)
        for c in range(3):
            cell_border(tbl.cell(r, c))
    footer(s, "Benefits summarised from the insurer brochures; in any conflict the policy wording prevails. "
              "Every row is traced in the audit report.", idx, total)
    notes(s, "\n\n".join(
        f"{b.risk}\n  Benefit: {b.matched_benefit}\n  Source: {b.source_policy}, p.{b.page_number}\n"
        f"  Quote: \"{b.exact_quote_from_source}\"" for b in rows))


def _slide_recommendation(deck: Deck, pitch, idx, total):
    s = deck.slide(NAVY)
    rec = pitch.recommended_policy
    text(s, MARGIN, 0.35, 5, 0.3, "OUR RECOMMENDATION", size=10, bold=True, color=MINT, char_spacing=2)
    name = clean(rec.policy_name)
    text(s, MARGIN, 0.62, 9, 0.6, name, size=fit_size(name, 9, 0.6, (28, 26, 24, 22, 20), char_w=0.52),
         font=HEAD, bold=True, color=WHITE, anchor=MSO_ANCHOR.MIDDLE)
    just = sentences(rec.justification) or [clean(rec.justification)]
    first = just[0]
    for candidate in (name, name.split("—")[-1].strip(), name.split(" - ")[-1].strip()):
        if candidate and first.lower().startswith(candidate.lower()):
            rest_of = first[len(candidate):].strip(" :-,—")
            first = ("It " + rest_of) if rest_of[:3].lower() in ("is ", "cov", "off", "pro", "has", "giv", "add") else rest_of
            first = first[:1].upper() + first[1:]
            break
    ls = fit_size(first, 9, 0.5, (13, 12.5, 12, 11.5, 11, 10.5))
    text(s, MARGIN, 1.25, 9, 0.5, shorten(first, 9, 0.5, ls), size=ls, color=PALE)

    points = [clean(p) for p in (rec.key_talking_points or [])][:4] or just[1:4]
    n = max(1, len(points))
    gap = 0.2
    card_w = (SLIDE_W - 2 * MARGIN - (n - 1) * gap) / n
    rest = " ".join(just[1:])
    max_h = 2.05 if rest else 2.6
    tw = card_w - 0.4
    size = common_size(points, tw, max_h - 0.8, (13, 12, 11.5, 11, 10.5, 10, 9.5, 9))
    need = max((text_height(p, tw, size) for p in points), default=0.5)
    card_h = min(max_h, max(1.5, need + 0.85))
    for i, p in enumerate(points):
        cx = MARGIN + i * (card_w + gap)
        rect(s, cx, 1.9, card_w, card_h, WHITE)
        circle(s, cx + 0.2, 2.07, 0.4, str(i + 1), fill=TEAL, size=11)
        th = card_h - 0.75
        text(s, cx + 0.2, 2.6, tw, th, shorten(p, tw, th, size), size=size, color=INK)

    if rest:
        y = 1.9 + card_h + 0.2
        h = 5.05 - y
        runs = [[("Why this plan over the others: ", {"bold": True, "color": MINT}), (rest, {"color": PALE})]]
        size = fit_size("Why this plan over the others: " + rest, 9, h, (11, 10.5, 10, 9.5, 9, 8.5))
        if _lines_needed("Why this plan over the others: " + rest, 9, size) * size * 1.18 / 72 > h:
            runs = [[("Why this plan over the others: ", {"bold": True, "color": MINT}),
                     (shorten(rest, 9, h, size), {"color": PALE})]]
        text(s, MARGIN, y, 9, h, runs, size=size)
    footer(s, "Recommendation subject to quotes, underwriting and the full policy wording.", idx, total, dark=True)
    notes(s, f"Full justification:\n{clean(rec.justification)}")


def _slide_sources(deck: Deck, pitch, profile, idx, total):
    s = deck.slide()
    title(s, "Sources and assumptions", "What this proposal is based on, and what the advisor should confirm first")

    # Left: sources
    rect(s, MARGIN, 1.4, 4.35, 3.55, SOFT)
    text(s, MARGIN + 0.25, 1.55, 3.9, 0.3, "Sources", size=14, font=HEAD, bold=True, color=NAVY)
    src_lines = ["Policy benefits: the four insurer brochures supplied (Aditya Birla Activ One, "
                 "Care Supreme, HDFC ERGO Optima Secure+, Niva Bupa ReAssure 2.0)."]
    web = list(getattr(profile, "sources", None) or [])
    if web:
        src_lines.append("Company figures (live web research):")
        src_lines += [f"[{w['id']}] {clean(w['title'])[:60]} — {short_url(w['url'])[:48]}" for w in web[:7]]
    else:
        src_lines.append("Company figures: no live web data — all company figures are assumptions.")
    body = "\n".join(src_lines)
    size = fit_size(body, 3.85, 2.9, (10.5, 10, 9.5, 9, 8.5, 8))
    text(s, MARGIN + 0.25, 1.95, 3.85, 2.9, [[(l, {"color": INK if not l.startswith("[") else MUTE})] for l in src_lines],
         size=size)

    # Right: assumptions
    x = MARGIN + 4.6
    text(s, x, 1.4, 4.4, 0.35, "Assumptions to confirm", size=14, font=HEAD, bold=True, color=NAVY,
         anchor=MSO_ANCHOR.MIDDLE)
    seen, items = set(), []
    for a in list(getattr(profile, "assumptions_used", None) or []) + list(pitch.assumptions_used or []):
        k = clean(a).lower()[:80]
        if k and k not in seen:
            seen.add(k)
            items.append(clean(a))
    items = items[:6] or ["No assumptions were recorded."]
    row_h = min(0.6, 3.15 / len(items))
    for i, a in enumerate(items):
        y = 1.85 + i * row_h
        circle(s, x, y + (row_h - 0.28) / 2, 0.28, str(i + 1), fill=CORAL, size=9.5)
        size = fit_size(a, 3.95, row_h - 0.04, (10.5, 10, 9.5, 9, 8.5, 8))
        text(s, x + 0.42, y, 3.95, row_h, shorten(a, 3.95, row_h - 0.04, size), size=size, color=INK,
             anchor=MSO_ANCHOR.MIDDLE)
    footer(s, "Full claim-by-claim traceability is in the accompanying audit report.", idx, total)
    notes(s, "All assumptions:\n" + "\n".join(f"- {a}" for a in items) +
          ("\n\nWeb sources:\n" + "\n".join(f"[{w['id']}] {w['title']} — {w['url']}" for w in web) if web else ""))


# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------

def render_deck(pitch, output_path: str, profile=None) -> int:
    """
    Renders `pitch` (PitchContent) to `output_path`. `profile` (CompanyProfile)
    is optional; when given, its sourced facts become stat cards and its web
    sources are listed. Returns the number of slides written (3-5).
    """
    deck = Deck()
    month = date.today().strftime("%B %Y")
    benefits = list(pitch.benefits or [])
    chunks = [benefits[i:i + MAX_BENEFIT_ROWS] for i in range(0, len(benefits), MAX_BENEFIT_ROWS)] or [[]]
    chunks = chunks[:2]  # never more than 2 benefit slides (keeps the deck within 5)
    include_sources = 3 + len(chunks) + 1 <= 5   # overview + why + benefits + rec (+ sources)
    total = 3 + len(chunks) + (1 if include_sources else 0)

    i = 1
    _slide_overview(deck, pitch, profile, i, total, month); i += 1
    _slide_why_marsh(deck, pitch, i, total); i += 1
    for k, rows in enumerate(chunks):
        _slide_benefits(deck, pitch, rows, i, total, cont=k > 0); i += 1
    _slide_recommendation(deck, pitch, i, total); i += 1
    if include_sources:
        _slide_sources(deck, pitch, profile, i, total)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    deck.prs.save(output_path)
    return total
