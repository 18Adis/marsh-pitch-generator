"""
Stage 6c — Human-readable audit report (.docx), max 3 A4 pages.

Sections (order follows the project's original report template):
  Title + document control
  1. Summary — automated audit (status, weighted confidence, counts) and the
     advisor's review from the app (reviewed by, date, approved / edited /
     removed, final status)
  2. Claim-by-Claim Results — every claim with its automated result, finding,
     source (document, page, match type) and the advisor's decision
  3. Statements Outside the Policy Documents — Marsh service, comparative
     reasoning, company facts, assumptions, with the advisor's confirm/remove

Human verification itself happens in the Streamlit UI (src/audit/review.py);
the report records the outcome. python-docx (no Node runtime); DOCX-skill
rules: CLEAR shading, explicit cell widths, schema-ordered XML, repeat
header rows. A height budget + density levels keep it within 3 pages.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

NAVY = "0B2545"
TEAL = "13807A"
SOFT = "F4F7FA"
LINE = "C9D3DE"
MUTE = RGBColor(0x5F, 0x6B, 0x7A)
INK = RGBColor(0x1F, 0x29, 0x33)

STATUS_STYLE = {   # status -> (label, text colour, fill)
    "VERIFIED": ("VERIFIED", "1E7E34", "E6F4EA"),
    "VERIFIED_WITH_CONDITION": ("VERIFIED – CONDITION", "1E6B5C", "E3F2EF"),
    "FLAGGED": ("FLAGGED", "9A6700", "FFF4D6"),
    "REJECTED": ("REJECTED", "B42318", "FDE8E7"),
    "UNCHECKED": ("UNCHECKED", "5F6B7A", "EEF1F4"),
}
OVERALL_STYLE = {
    "PASS": ("1E7E34", "E6F4EA"),
    "PASS WITH CONDITIONS": ("1E6B5C", "E3F2EF"),
    "NEEDS HUMAN REVIEW": ("9A6700", "FFF4D6"),
    "REJECT — REGENERATE": ("B42318", "FDE8E7"),
    "AUDIT INCOMPLETE": ("5F6B7A", "EEF1F4"),
    "NO CLAIMS TO AUDIT": ("5F6B7A", "EEF1F4"),
}
FINAL_STYLE = {
    "APPROVED FOR CLIENT": ("1E7E34", "E6F4EA"),
    "APPROVED WITH ADVISOR OVERRIDES": ("9A6700", "FFF4D6"),
    "NO CLAIMS LEFT — REGENERATE": ("B42318", "FDE8E7"),
}
FINAL_TEXT = {
    "APPROVED FOR CLIENT": "Every claim left in the final pitch is either verified against the source brochures, "
                           "checked manually by the advisor, or edited and re-verified. The final deck reflects these decisions.",
    "APPROVED WITH ADVISOR OVERRIDES": "The advisor kept at least one edited claim that the automated re-check did not "
                                       "support (marked in red below). The advisor takes responsibility for that wording.",
    "NO CLAIMS LEFT — REGENERATE": "The advisor removed every policy claim. Regenerate the pitch.",
}
DECISION_TEXT = {
    "PASS": "Every policy claim is verified against the source brochures. The advisor may approve the pitch.",
    "PASS WITH CONDITIONS": "All policy claims are grounded; some need a minor edit (a condition to add or a citation to "
                            "correct).",
    "NEEDS HUMAN REVIEW": "One or more claims are overstated, altered or unsupported and must be edited or removed.",
    "REJECT — REGENERATE": "Half or more of the policy claims cannot be supported. Do not send; regenerate the pitch.",
    "AUDIT INCOMPLETE": "The independent check could not run for some claims (see notes). Re-run the audit; do not rely "
                        "on the score until every claim has been checked.",
    "NO CLAIMS TO AUDIT": "The pitch contained no policy claims. Review the non-policy statements in Section 3.",
}
QUOTE_STATUS_TEXT = {
    "EXACT_CLAIMED_PAGE": "Exact quote, cited page",
    "STITCHED_CLAIMED_PAGE": "Quote found, cited page",
    "EXACT_OTHER_PAGE": "Exact quote, different page",
    "STITCHED_OTHER_PAGE": "Quote found, different page",
    "FUZZY_CLAIMED_PAGE": "Similar wording, cited page",
    "FUZZY_OTHER_PAGE": "Similar wording, different page",
    "FOUND_IN_OTHER_DOC": "Found in a different brochure",
    "RETRIEVED_EVIDENCE": "Evidence retrieved",
    "NOT_FOUND_ANYWHERE": "Not found in any brochure",
    "UNKNOWN_SOURCE": "Unknown policy",
    "NO_EVIDENCE": "No evidence found",
}
DOC_NAMES = {
    "ABHI_Product_Brochure.pdf": "Aditya Birla Activ One",
    "Care_Health_Product_Brochure.pdf": "Care Supreme",
    "HDFC_Product_Brochure.pdf": "HDFC ERGO Optima Secure+",
    "Niva_Bupa_Product_Brochure.pdf": "Niva Bupa ReAssure 2.0",
}


# ------------------------------------------------------------
# XML helpers
# ------------------------------------------------------------

def _shade(cell, fill: str):
    tc_pr = cell._tc.get_or_add_tcPr()
    for old in tc_pr.findall(qn("w:shd")):
        tc_pr.remove(old)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.insert_element_before(shd, "w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText", "w:vAlign", "w:hideMark",
                                "w:headers", "w:cellIns", "w:cellDel", "w:cellMerge", "w:tcPrChange")


def _borders(table, color=LINE, size=6):
    tbl_pr = table._tbl.tblPr
    b = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{edge}")
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), str(size))
        e.set(qn("w:space"), "0")
        e.set(qn("w:color"), color)
        b.append(e)
    tbl_pr.insert_element_before(b, "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook", "w:tblCaption",
                                 "w:tblDescription", "w:tblPrChange")
    # fixed layout (honours column widths) is set by table.autofit = False


def _repeat_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    h = OxmlElement("w:tblHeader")
    h.set(qn("w:val"), "true")
    tr_pr.append(h)


def _no_split(row):
    tr_pr = row._tr.get_or_add_trPr()
    c = OxmlElement("w:cantSplit")
    c.set(qn("w:val"), "true")
    tr_pr.append(c)


def _page_border(section, color=NAVY):
    sect_pr = section._sectPr
    pg = OxmlElement("w:pgBorders")
    pg.set(qn("w:offsetFrom"), "page")
    for edge in ("top", "left", "bottom", "right"):
        e = OxmlElement(f"w:{edge}")
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), "12")
        e.set(qn("w:space"), "24")
        e.set(qn("w:color"), color)
        pg.append(e)
    # schema order: pgBorders must come after pgMar
    pg_mar = sect_pr.find(qn("w:pgMar"))
    if pg_mar is not None:
        pg_mar.addnext(pg)
    else:
        sect_pr.append(pg)


def _field(run, instr: str):
    for tag, text in (("begin", None), (None, instr), ("separate", None), (None, "1"), ("end", None)):
        if tag:
            f = OxmlElement("w:fldChar")
            f.set(qn("w:fldCharType"), tag)
            run._r.append(f)
        elif text == instr:
            t = OxmlElement("w:instrText")
            t.set(qn("xml:space"), "preserve")
            t.text = f" {instr} "
            run._r.append(t)
        else:
            t = OxmlElement("w:t")
            t.text = text
            run._r.append(t)


def _para_border_bottom(p, color=NAVY, size=8):
    p_pr = p._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    b = OxmlElement("w:bottom")
    b.set(qn("w:val"), "single")
    b.set(qn("w:sz"), str(size))
    b.set(qn("w:space"), "4")
    b.set(qn("w:color"), color)
    bdr.append(b)
    p_pr.insert_element_before(bdr, "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
                               "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi",
                               "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing",
                               "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
                               "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr",
                               "w:pPrChange")


# ------------------------------------------------------------
# Content helpers
# ------------------------------------------------------------

def _run(p, text, size=10, bold=False, color=INK, italic=False, font=None):
    r = p.add_run(text)
    r.font.size = Pt(size)
    r.bold = bold
    r.italic = italic
    r.font.color.rgb = color if isinstance(color, RGBColor) else RGBColor.from_string(color)
    if font:
        r.font.name = font
    return r


def _cell(cell, text, size=9, bold=False, color=INK, fill=None, align=WD_ALIGN_PARAGRAPH.LEFT, width=None):
    cell.text = ""
    p = cell.paragraphs[0]
    p.alignment = align
    p.paragraph_format.space_after = Pt(0)
    lines = str(text).split("\n")
    for i, line in enumerate(lines):
        if i:
            p = cell.add_paragraph()
            p.alignment = align
            p.paragraph_format.space_after = Pt(0)
        _run(p, line, size=size, bold=bold, color=color)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    if fill:
        _shade(cell, fill)
    if width is not None:
        cell.width = Inches(width)


def _grid(t, widths):
    for i, w in enumerate(widths):
        t.columns[i].width = Inches(w)


def _table(doc, headers, widths, header_fill=NAVY, size=9):
    t = doc.add_table(rows=1, cols=len(headers))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    _borders(t)
    _grid(t, widths)
    for i, (h, w) in enumerate(zip(headers, widths)):
        _cell(t.rows[0].cells[i], h, size=size, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF), fill=header_fill, width=w)
    _repeat_header(t.rows[0])
    return t


def _add_row(t, values, widths, fills=None, colors=None, bolds=None, size=9):
    row = t.add_row()
    _no_split(row)
    for i, (v, w) in enumerate(zip(values, widths)):
        _cell(row.cells[i], v, size=size, width=w,
              fill=(fills or {}).get(i), color=(colors or {}).get(i, INK), bold=(bolds or {}).get(i, False))
    return row


def _heading(doc, text, size=13):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(4)
    p.paragraph_format.keep_with_next = True
    _run(p, text, size=size, bold=True, color=NAVY, font="Cambria")
    _para_border_bottom(p, color=LINE, size=6)
    return p


def _body(doc, text, size=10, color=INK, italic=False, space_after=4):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    _run(p, text, size=size, color=color, italic=italic)
    return p


def _source_label(r) -> str:
    from src.audit.verifier import resolve_source_document
    doc, page = r.get("matched_document"), r.get("matched_page")
    found = DOC_NAMES.get(doc or "", "")
    how = QUOTE_STATUS_TEXT.get(r.get("quote_status", ""), r.get("quote_status", ""))
    if r.get("claim_type") == "talking_point":
        return (f"{found}, p.{page}" if found and page else found or r.get("source_policy", "")) + f"\n{how}"
    cited_doc = resolve_source_document(r.get("source_policy", ""))
    cited = f"{DOC_NAMES.get(cited_doc or '', r.get('source_policy', ''))}, p.{r.get('page_number')}"
    if not doc or (cited_doc == doc and page == r.get("page_number")):
        return f"{cited}\n{how}"
    return f"Cited: {cited}\nFound: {found}, p.{page}\n{how}"


def _short_url(u: str, n: int = 48) -> str:
    import re as _re
    u = _re.sub(r"^https?://(www\.)?", "", u or "")
    return u if len(u) <= n else u[:n - 1] + "…"


# ------------------------------------------------------------
# Height budget (keeps the report to MAX_PAGES)
# ------------------------------------------------------------
# python-docx cannot measure text, so each block's height is estimated
# (Calibri ~0.48 em average glyph width). The builder is run at increasing
# density levels until the estimate fits; tests render real worst-case
# reports with LibreOffice to confirm the result is <= 3 pages.

MAX_PAGES = 3
SAFETY_PAGES = 0.45     # calibrated against LibreOffice renders: the estimate can run ~0.4 page short
PAGE_W, PAGE_H = 8.27, 11.69                      # A4
MARGIN_LR, MARGIN_T, MARGIN_B = 0.7, 0.8, 0.75
CONTENT_W = PAGE_W - 2 * MARGIN_LR
USABLE_H = PAGE_H - MARGIN_T - MARGIN_B - 0.35    # minus header/footer allowance
PAGE_SLACK = 0.55                                  # rows can't split: space lost at page ends


def _lines(text: str, width_in: float, pt: float) -> int:
    cpl = max(8, int(width_in * 72 / (pt * 0.48)))
    n = 0
    for para in str(text or "").split("\n"):
        n += max(1, -(-len(para) // cpl))
    return n


def _h(text, width_in, pt, spacing=1.15):
    return _lines(text, width_in, pt) * pt * spacing / 72


class Budget:
    def __init__(self):
        self.h = 0.0

    def para(self, text, pt, width=CONTENT_W, before=0.0, after=4):
        self.h += _h(text, width, pt) + (before + after) / 72

    def row(self, cells, pt, pad=0.07):
        """cells: [(text, width_in)]"""
        self.h += max(_h(t, w - 0.15, pt) for t, w in cells) + pad

    @property
    def pages(self) -> float:
        return self.h / (USABLE_H - PAGE_SLACK)


def _clip(text: str, n: int | None) -> str:
    t = " ".join(str(text or "").split())
    if not n or len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0].rstrip(",;:—-")
    return cut + "…"


# density levels: 0 = most detail ... 3 = most compact
LEVELS = [
    dict(claim=None, reason=220, facts_rows=True, body=9, table=8.5),
    dict(claim=220, reason=160, facts_rows=True, body=9, table=8.5),
    dict(claim=160, reason=120, facts_rows=False, body=8.5, table=8),
    dict(claim=120, reason=90, facts_rows=False, body=8.5, table=7.5, others_max=5),
    dict(claim=90, reason=70, facts_rows=False, body=8, table=7.5, others_max=4),
    dict(claim=70, reason=50, facts_rows=False, body=8, table=7, others_max=3),
]


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def generate_audit_docx(company_name: str, pitch: dict, audit: dict, output_path: str,
                        audit_ref: str | None = None, deck_file: str | None = None) -> int:
    """
    Writes the audit report (max 3 A4 pages). Returns the density level used
    (0 = full detail). Full, untrimmed text always remains in the audit JSON.
    """
    for level, cfg in enumerate(LEVELS):
        doc, budget = _build(company_name, pitch, audit, cfg, audit_ref, deck_file)
        if budget.pages <= MAX_PAGES - SAFETY_PAGES or level == len(LEVELS) - 1:
            break
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)
    return level


def _build(company_name, pitch, audit, cfg, audit_ref, deck_file):
    B = Budget()
    now = datetime.now()
    ref = audit_ref or f"AUD-{''.join(ch for ch in company_name.upper() if ch.isalnum())[:8]}-{now:%Y%m%d-%H%M}"
    rec_name = (pitch.get("recommended_policy") or {}).get("policy_name", "—")
    body, tpt = cfg["body"], cfg["table"]

    doc = Document()
    zoom = doc.settings.element.find(qn("w:zoom"))
    if zoom is not None and zoom.get(qn("w:percent")) is None:
        zoom.set(qn("w:percent"), "100")
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(body)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    st.paragraph_format.space_after = Pt(2)

    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.PORTRAIT
    sec.page_width, sec.page_height = Inches(PAGE_W), Inches(PAGE_H)
    sec.left_margin = sec.right_margin = Inches(MARGIN_LR)
    sec.top_margin, sec.bottom_margin = Inches(MARGIN_T), Inches(MARGIN_B)
    sec.header_distance = sec.footer_distance = Inches(0.4)
    _page_border(sec)
    W = CONTENT_W

    hp = sec.header.paragraphs[0]
    _run(hp, f"Insurance Pitch Audit Report  ·  {company_name}", size=7.5, color=MUTE)
    _run(hp, f"\t\t{ref}", size=7.5, color=MUTE)
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(fp, "Confidential — for advisor review  ·  Page ", size=7.5, color=MUTE)
    _field(_run(fp, "", size=7.5, color=MUTE), "PAGE")
    _run(fp, " of ", size=7.5, color=MUTE)
    _field(_run(fp, "", size=7.5, color=MUTE), "NUMPAGES")

    # ---------------- Title block (template order) ----------------
    t = doc.add_paragraph()
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    t.paragraph_format.space_after = Pt(0)
    _run(t, f"Insurance Pitch Audit Report — {company_name}", size=17, bold=True, color=NAVY, font="Cambria")
    B.para("x" * len(f"Insurance Pitch Audit Report — {company_name}"), 17, after=0)
    s_ = doc.add_paragraph()
    s_.alignment = WD_ALIGN_PARAGRAPH.CENTER
    s_.paragraph_format.space_after = Pt(6)
    _run(s_, f"Recommended policy: {rec_name}", size=10, italic=True, color=MUTE)
    _para_border_bottom(s_, color=NAVY, size=10)
    B.para(rec_name, 10, after=8)

    # Document control — 4 columns (label/value x2) to save height
    dc = [("Audit reference", ref), ("Date of audit", audit.get("audited_at") or now.strftime("%d %b %Y, %H:%M")),
          ("Client", company_name), ("Pitch deck", deck_file or f"{company_name}_Pitch.pptx"),
          ("Independent checker", audit.get("audit_model") or "—"),
          ("Policy baseline", "Activ One · Care Supreme · Optima Secure+ · ReAssure 2.0")]
    wdc = [1.15, W / 2 - 1.15, 1.15, W / 2 - 1.15]
    tbl = doc.add_table(rows=0, cols=4)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False
    _borders(tbl)
    _grid(tbl, wdc)
    for i in range(0, len(dc), 2):
        row = tbl.add_row()
        for j, (k, v) in enumerate(dc[i:i + 2]):
            _cell(row.cells[j * 2], k, size=tpt, bold=True, color=RGBColor.from_string(NAVY), fill=SOFT, width=wdc[j * 2])
            _cell(row.cells[j * 2 + 1], v, size=tpt, width=wdc[j * 2 + 1])
        B.row([(dc[i][1], wdc[1]), (dc[i + 1][1], wdc[3])], tpt)

    # ---------------- 1. Summary ----------------
    _heading(doc, "1. Summary", 12)
    B.para("1. Summary", 12, before=10, after=5)
    status = audit.get("overall_status", "")
    col, fill = OVERALL_STYLE.get(status, ("1F2933", "FFFFFF"))
    lab = _body(doc, "Automated audit", size=body - 0.5, color=MUTE, space_after=2)
    B.para("Automated audit", body - 0.5, after=2)
    heads = ["Overall Status", "Confidence Score", "Verified", "Verified – condition", "Flagged", "Rejected", "Unchecked"]
    ws = [1.75, 1.0, 0.72, 0.95, 0.72, 0.72, W - (1.75 + 1.0 + 0.72 + 0.95 + 0.72 * 2)]
    sm = _table(doc, heads, ws, size=tpt)
    vals = [status, f"{audit.get('confidence_score', 0)}%", str(audit.get("verified_count", 0)),
            str(audit.get("conditional_count", 0)), str(audit.get("flagged_count", 0)),
            str(audit.get("rejected_count", 0)), str(audit.get("unchecked_count", 0))]
    _add_row(sm, vals, ws, fills={0: fill}, colors={0: RGBColor.from_string(col)}, bolds={0: True, 1: True}, size=body)
    for c_ in sm.rows[1].cells[1:]:
        c_.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    B.row([(h, w) for h, w in zip(heads, ws)], tpt)
    B.row([(v, w) for v, w in zip(vals, ws)], body)

    results = audit.get("claim_results") or []
    reviewed = audit.get("review_status") == "COMPLETED"
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(2)
    _run(p, "Advisor review (human verification in the app)", size=body - 0.5, color=MUTE)
    B.para("Advisor review", body - 0.5, before=8, after=2)
    if reviewed:
        dec = [r.get("advisor_decision", "") for r in results]
        ok_edits = sum(1 for r in results if r.get("advisor_decision") == "edited"
                       and r.get("recheck_status") in ("VERIFIED", "VERIFIED_WITH_CONDITION"))
        final = audit.get("final_status", "")
        fcol, ffill = FINAL_STYLE.get(final, ("1F2933", "FFFFFF"))
        rh = ["Final Status", "Reviewed by", "Date", "Auto-verified", "Approved", "Edited", "Removed"]
        rw = [1.75, 1.15, 1.0, 0.8, 0.78, 0.72, W - (1.75 + 1.15 + 1.0 + 0.8 + 0.78 + 0.72)]
        rt = _table(doc, rh, rw, size=tpt)
        rv = [final, audit.get("reviewed_by", ""), audit.get("reviewed_at", ""), str(dec.count("auto-verified")),
              str(dec.count("approved")), f"{dec.count('edited')}" + (f" ({ok_edits} re-verified)" if dec.count("edited") else ""),
              str(dec.count("removed"))]
        _add_row(rt, rv, rw, fills={0: ffill}, colors={0: RGBColor.from_string(fcol)}, bolds={0: True}, size=body)
        B.row([(h, w) for h, w in zip(rh, rw)], tpt)
        B.row([(v, w) for v, w in zip(rv, rw)], body)
        outcome = FINAL_TEXT.get(final, "")
    else:
        outcome = ("Pending — the advisor has not yet reviewed the flagged items. Complete the Human Verification "
                   "step in the app before this pitch is sent. " + DECISION_TEXT.get(status, ""))
    q = doc.add_paragraph()
    q.paragraph_format.space_before = Pt(5)
    _run(q, "Outcome: ", bold=True, color=NAVY, size=body)
    _run(q, outcome, size=body)
    B.para("Outcome: " + outcome, body, before=5)
    note = (f"{audit.get('total_claims', 0)} policy claim(s) audited. Confidence = weighted average over checked claims "
            f"(verified 100%, with condition 80%, flagged 40%, rejected 0%); "
            f"{audit.get('fully_verified_pct', 0)}% fully verified without any edit.")
    _body(doc, note, size=body - 1, color=MUTE)
    B.para(note, body - 1)
    for n in audit.get("notes") or []:
        np_ = doc.add_paragraph()
        _run(np_, "Note: ", bold=True, color="9A6700", size=body)
        _run(np_, n, size=body)
        B.para("Note: " + n, body)

    # ---------------- 2. Claim-by-claim ----------------
    _heading(doc, "2. Claim-by-Claim Results", 12)
    B.para("2", 12, before=10, after=5)
    if not results:
        _body(doc, "No claims were generated to audit.", size=body)
        B.para("No claims", body)
    else:
        ws2 = [0.36, 0.98, W - (0.36 + 0.98 + 1.4 + 1.1), 1.4, 1.1]
        ct = _table(doc, ["ID", "Automated result", "Claim in the pitch", "Source", "Advisor decision"], ws2, size=tpt)
        B.row([("ID", ws2[0])], tpt)
        for r in results:
            label, tcol, tfill = STATUS_STYLE.get(r["final_status"], (r["final_status"], "1F2933", "FFFFFF"))
            where = "Recommendation" if r.get("claim_type") == "talking_point" else f"Benefit · {_clip(r.get('risk', ''), 50)}"
            decision = r.get("advisor_decision", "")
            if decision == "edited":
                main = f"{_clip(r.get('advisor_text', ''), cfg['claim'])}"
                sub = f"Edited. Original: {_clip(r.get('matched_benefit', ''), 90)}"
            else:
                main = _clip(r.get("matched_benefit", ""), cfg["claim"])
                sub = ""
            finding = ""
            if r["final_status"] != "VERIFIED" and r.get("semantic_reason"):
                finding = "Finding: " + _clip(r["semantic_reason"], cfg["reason"])
                if r.get("missing_condition"):
                    finding += f" (add: {_clip(r['missing_condition'], 60)})"
            lines_ = [main] + [x for x in (sub, finding, where) if x]
            claim_txt = "\n".join(lines_)
            dtxt, dcol, dfill = _decision_cell(r, reviewed)
            row = _add_row(ct, [r.get("claim_id", ""), label, claim_txt, _source_label(r), dtxt], ws2,
                           fills={1: tfill, 4: dfill}, colors={1: RGBColor.from_string(tcol), 3: MUTE,
                                                              4: RGBColor.from_string(dcol)},
                           bolds={0: True, 1: True, 4: True}, size=tpt)
            paras = row.cells[2].paragraphs
            for para in paras[1:]:                     # secondary lines muted + smaller
                for run in para.runs:
                    run.font.color.rgb = MUTE
                    run.font.size = Pt(tpt - 0.5)
            if decision == "removed":
                for run in paras[0].runs:
                    run.font.strike = True
                    run.font.color.rgb = MUTE
            B.row([(claim_txt, ws2[2]), (_source_label(r), ws2[3]), (dtxt, ws2[4])], tpt)

    # ---------------- 3. Other statements ----------------
    others = audit.get("other_statements") or []
    _heading(doc, "3. Statements Outside the Policy Documents (not scored)", 12)
    B.para("3", 12, before=10, after=5)
    intro = ("No policy clause can prove these; the advisor confirms or removes each one in the app "
             "before the deck is finalised.")
    _body(doc, intro, size=body - 1, color=MUTE)
    B.para(intro, body - 1)
    rows = _group_others(others, cfg, reviewed)
    if rows:
        ws4 = [1.2, W - (1.2 + 1.45), 1.45]
        ot = _table(doc, ["Category", "Statement(s)", "Status"], ws4, size=tpt)
        B.row([("C", ws4[0])], tpt)
        for cat, stmt, status_txt, warn in rows:
            _add_row(ot, [cat, stmt, status_txt], ws4,
                     colors={2: RGBColor.from_string("B42318") if warn else INK}, bolds={0: True}, size=tpt)
            B.row([(stmt, ws4[1]), (status_txt, ws4[2]), (cat, ws4[0])], tpt)
    else:
        _body(doc, "None.", size=body)
        B.para("None", body)
    return doc, B


def _decision_cell(r: dict, reviewed: bool) -> tuple[str, str, str]:
    """(text, colour, fill) for the advisor-decision column."""
    d = r.get("advisor_decision", "")
    if not reviewed or not d:
        return ("Pending review", "9A6700", "FFF4D6") if r["final_status"] != "VERIFIED" else ("—", "5F6B7A", "FFFFFF")
    if d == "auto-verified":
        return "Auto-verified", "1E7E34", "E6F4EA"
    if d == "approved":
        return "Approved (checked manually)", "1E6B5C", "E3F2EF"
    if d == "removed":
        return "Removed from pitch", "5F6B7A", "EEF1F4"
    if d == "edited":
        rs = r.get("recheck_status", "")
        if rs in ("VERIFIED", "VERIFIED_WITH_CONDITION"):
            return "Edited — re-check passed", "1E7E34", "E6F4EA"
        if rs in ("FLAGGED", "REJECTED"):
            return f"Edited — re-check {rs.lower()}; kept by advisor", "B42318", "FDE8E7"
        return "Edited — re-check not run", "9A6700", "FFF4D6"
    return d, "1F2933", "FFFFFF"


def _group_others(others: list[dict], cfg, reviewed: bool = False) -> list[tuple[str, str, str, bool]]:
    """Rows for section 3: (category, statement text, status, is_warning)."""
    by: dict[str, list[dict]] = {}
    for o in others:
        by.setdefault(o["category"], []).append(o)
    rows = []
    order = ["Marsh service", "Comparative reasoning", "Company fact", "Assumption"]
    cap = cfg.get("others_max")
    for cat in order + [c for c in by if c not in order]:
        items = by.get(cat) or []
        if not items:
            continue
        shown = items[:cap] if cap else items
        bullets = []
        for o in shown:
            txt = _clip(o["statement"], 110 if cat == "Company fact" else cfg["claim"])
            if cat == "Company fact" and cfg["facts_rows"] and o.get("source"):
                sid, _, url = o["source"].partition(" ")
                txt += f"  ({sid} {_short_url(url, 36)})"
            if "NOT verified" in o.get("status", ""):
                txt += "  — NOT verified in source"
            if reviewed and o.get("advisor_decision") == "removed":
                txt += "  [removed]"
            bullets.append(("• " if len(items) > 1 else "") + txt)
        if cap and len(items) > cap:
            bullets.append(f"+ {len(items) - cap} more (full list in the audit data file)")
        if reviewed and any(o.get("advisor_decision") for o in items):
            n_keep = sum(1 for o in items if o.get("advisor_decision") == "confirmed")
            n_rm = sum(1 for o in items if o.get("advisor_decision") == "removed")
            status = f"Advisor: {n_keep} confirmed" + (f", {n_rm} removed" if n_rm else "")
            warn = False
        else:
            unverified = any("NOT verified" in o.get("status", "") for o in items)
            status = ("Some figures NOT verified — do not quote" if unverified
                      else "Pending advisor confirmation" if cat != "Company fact" else "Verified in web sources")
            warn = unverified
        rows.append((cat, "\n".join(bullets), status, warn))
    return rows
