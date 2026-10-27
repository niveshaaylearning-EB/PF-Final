"""Builds the merged result-update PDF (see routers/result_updates.py for
the full context). Per company, in this exact order (matching the real
sample format, confirmed against MID&SM~1.PDF / MID&SM~2.PDF, 2026-10-23):
  company name (bold) -> [optional financial table, auto-parsed from the
  uploaded result-update document by result_update_doc_parser.py -- NOT
  every company has one] -> "Operational Performance:" + bold-lead-in
  bullets -> "Outlook:" + bold-lead-in bullets.
Basket name appears once as the document title. The SEBI research-analyst
disclosure footer appears once, at the very end, not per company -- it's
fixed legal boilerplate, not something to repeat per page.

The financial table's row/column structure is NOT fixed -- confirmed live
that different companies' tables don't share the same line items (e.g.
"Cost of materials consumed" vs "Cost of raw materials and components
consumed" vs separate "Purchases of stock-in-trade" + "Changes in
inventories" rows). It's rendered generically from whatever headers/rows
the source document's table actually had, with a row rendered bold+shaded
when its label was bold in the source (subtotal/margin lines like Gross
Profit, EBITDA, PAT) -- detected from real formatting, not guessed by label
name. A company whose financial snapshot instead arrived as a pasted-in
picture (not a real Word table) gets that picture embedded directly
instead -- see _company_flowables.

Every page carries a light green border frame and the Niveshaay logo in
the top-right corner (see _page_decoration), matching the real reference
sample -- the logo asset lives at assets/niveshaay_logo.png, cropped
2026-10-27 from a reference document the firm itself provided (there was
no logo asset available before that).
"""
import html
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, Image,
)

_GREEN = colors.HexColor("#456232")
_PAGE_BORDER_GREEN = colors.HexColor("#D7E4D3")
_TABLE_HEADER_BG = colors.HexColor("#1F3864")   # dark navy, matches the sample's column-header row
_TABLE_ROW_LIGHT = colors.HexColor("#E2EFDA")   # light mint green, regular line items
_TABLE_ROW_BOLD = colors.HexColor("#A9D18E")    # darker green, subtotal/margin rows
_BOX_BORDER = colors.HexColor("#456232")
_BOX_ALT_ROW = colors.HexColor("#F2F6F0")

_LOGO_PATH = Path(__file__).parent / "assets" / "niveshaay_logo.png"

_styles = getSampleStyleSheet()
_basket_title_style = ParagraphStyle("BasketTitle", parent=_styles["Title"], fontSize=18, textColor=_GREEN, spaceAfter=4)
_consolidated_subtitle_style = ParagraphStyle("ConsolidatedSubtitle", parent=_styles["Normal"], fontSize=11, textColor=colors.black, alignment=1, spaceAfter=14)
_company_style = ParagraphStyle("Company", parent=_styles["Heading1"], fontSize=15, textColor=colors.black, spaceBefore=10, spaceAfter=8)
_section_style = ParagraphStyle("Section", parent=_styles["Heading2"], fontSize=12, textColor=_GREEN, spaceBefore=10, spaceAfter=6)
_bullet_style = ParagraphStyle("Bullet", parent=_styles["Normal"], fontSize=10, leading=14)
_footer_heading_style = ParagraphStyle("FooterHeading", parent=_styles["Heading3"], fontSize=11, textColor=colors.black, spaceBefore=10, spaceAfter=4)
_footer_body_style = ParagraphStyle("FooterBody", parent=_styles["Normal"], fontSize=8, leading=11)
_footer_subheading_style = ParagraphStyle("FooterSubheading", parent=_styles["Normal"], fontSize=8.5, leading=11, fontName="Helvetica-BoldOblique", spaceBefore=6, spaceAfter=3)
_footer_numbered_style = ParagraphStyle("FooterNumbered", parent=_styles["Normal"], fontSize=8, leading=11, leftIndent=14, firstLineIndent=-14, spaceAfter=4)
_table_header_style = ParagraphStyle("TableHeader", parent=_styles["Normal"], fontSize=8.5, textColor=colors.white, fontName="Helvetica-Bold")
_table_cell_style = ParagraphStyle("TableCell", parent=_styles["Normal"], fontSize=8.5, textColor=colors.black)
_table_cell_bold_style = ParagraphStyle("TableCellBold", parent=_styles["Normal"], fontSize=8.5, textColor=colors.black, fontName="Helvetica-Bold")


def _page_decoration(canvas, doc):
    """Drawn on every page: a light green border frame + the Niveshaay logo
    in the top-right corner, matching the real reference sample (confirmed
    live 2026-10-27 that neither was appearing in our output)."""
    canvas.saveState()
    w, h = A4
    margin = 0.7 * cm
    canvas.setStrokeColor(_PAGE_BORDER_GREEN)
    canvas.setLineWidth(2)
    canvas.rect(margin, margin, w - 2 * margin, h - 2 * margin)
    if _LOGO_PATH.exists():
        logo_w = 3.2 * cm
        logo_h = logo_w * (577 / 1186)  # the cropped asset's own aspect ratio
        canvas.drawImage(str(_LOGO_PATH), w - margin - logo_w - 0.3 * cm, h - margin - logo_h - 0.3 * cm,
                          width=logo_w, height=logo_h, preserveAspectRatio=True, mask="auto")
    canvas.restoreState()


def _sanitize(text: str) -> str:
    """Text pulled from an uploaded Word doc is untrusted-ish free text, fed
    straight into reportlab Paragraphs, which interpret a small HTML-like
    markup subset -- a literal "&"/"<"/">" in a company's own wording (e.g.
    "R&D") would otherwise break rendering. Also swaps the Rupee sign (₹)
    for "Rs." -- confirmed live that reportlab's base Helvetica font has no
    glyph for U+20B9 and silently renders it as a solid black square;
    embedding a Unicode-complete TTF just for this one symbol wasn't worth
    the cross-platform font-availability risk (this needs to render
    identically in the Linux production container too)."""
    return html.escape(text.replace("₹", "Rs. "), quote=False)


def _financial_table_flowable(financial_table: dict, max_width: float) -> Table:
    headers = financial_table.get("headers") or []
    data_rows = financial_table.get("rows") or []
    if not headers:
        return None

    table_data = [[Paragraph(_sanitize(h), _table_header_style) for h in headers]]
    for row in data_rows:
        cells = row.get("cells") or []
        style = _table_cell_bold_style if row.get("bold") else _table_cell_style
        table_data.append([Paragraph(_sanitize(c), style) for c in cells])

    n_cols = len(headers)
    first_col_width = max_width * 0.34
    other_col_width = (max_width - first_col_width) / max(n_cols - 1, 1)
    col_widths = [first_col_width] + [other_col_width] * (n_cols - 1)

    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), _TABLE_HEADER_BG),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]
    for i, row in enumerate(data_rows, start=1):
        bg = _TABLE_ROW_BOLD if row.get("bold") else _TABLE_ROW_LIGHT
        style_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))

    tbl = Table(table_data, colWidths=col_widths)
    tbl.setStyle(TableStyle(style_cmds))
    return tbl


def _bullets_box_flowable(bullets: list, max_width: float) -> Table:
    """Operational Performance / Outlook bullets rendered as rows in a single
    bordered, alternately-shaded box -- replaces the old plain-paragraph
    list, which ran down the page as an unbroken, unbounded list (confirmed
    live 2026-10-27 -- "this extends the list unnecessarily... needs a
    proper box like review that makes it more professional")."""
    if not bullets:
        return None
    rows = []
    for b in bullets:
        heading = _sanitize((b.get("heading") or "").strip())
        text = _sanitize((b.get("text") or "").strip())
        body = f"<b>{heading}:</b> {text}" if heading else text
        rows.append([Paragraph(body, _bullet_style)])
    tbl = Table(rows, colWidths=[max_width])
    style_cmds = [
        ("BOX", (0, 0), (-1, -1), 1, _BOX_BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9D6C3")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]
    for i in range(len(rows)):
        if i % 2 == 1:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), _BOX_ALT_ROW))
    tbl.setStyle(TableStyle(style_cmds))
    return tbl


def _snapshot_image_flowables(image_paths: list, max_width: float) -> list:
    """A financial snapshot pasted in as a picture (not a real Word table) --
    confirmed live 2026-10-27 this was being silently dropped entirely.
    Embedded at full available width, preserving its own aspect ratio."""
    flow = []
    for path in image_paths:
        if not path.exists():
            continue
        try:
            from PIL import Image as _PILImage
            with _PILImage.open(path) as im:
                w, h = im.size
        except Exception:
            continue
        scaled_h = max_width * (h / w)
        flow.append(Image(str(path), width=max_width, height=scaled_h))
        flow.append(Spacer(1, 10))
    return flow


# Structured as (heading | None, body text | list-of-items) so a numbered
# sub-list (Disciplinary History / Disclosures.../ Disclaimer) can be
# rendered as an actual numbered list -- matching the firm's real reference
# format -- instead of one run-on paragraph with "1. ... 2. ..." crammed
# inline, which is how the old flat-string version rendered (confirmed live
# 2026-10-27, "the sequencing is not proper at all").
_DISCLOSURE_SECTIONS = [
    (None, "Disclosure: The particulars given in this Disclosure Document have been prepared in accordance with SEBI (Research Analyst) Regulations, 2014. The purpose of the Document is to provide essential information about the research and recommendation services in a manner to assist and enable the prospective client/client in making an informed decision for engaging in research and recommendation services before investing. For the purpose of this Disclosure Document, Research Analyst is Niveshaay Investment Advisors Private Limited (hereinafter referred as &ldquo;Research Analyst&rdquo;)"),
    (None, "Business Activity: Research Analyst is registered with SEBI as Research Analyst with Registration No. INH000027830 &amp; BSE Enlistment No. 7247. The firm got its registration on 04/06/2026 and is engaged in research and recommendation Services. The focus of Research Analyst is to provide research and recommendations services to the clients. Analyst aligns its interests with those of the client and seeks to provide the best suited services."),
    (None, "Terms and conditions: The Research Report or Research Recommendation is issued to registered client. The Research Report /Recommendation is based on fundamental analysis. The Research Report/Recommendation is prepared solely for informational purpose and does not constitute an offer document or solicitation to buy or sell or subscribe for securities or other financial instruments for clients."),
    ("Disciplinary History:", [
        "No penalties / directions have been issued by SEBI under the SEBI Act or Regulations made there under against the Research Analyst relating to Research Analyst services.",
        "There are no pending material litigations or legal proceedings, findings of inspections or investigations for which action has been taken or initiated by any regulatory authority against the Research Analyst or its employees.",
    ]),
    ("Disclosures with respect to Research Reports and Research Recommendations Services:", [
        "The Research Analyst or its associates or relatives may have financial interest in the subject company.",
        "The Research Analyst or its associates or relatives, may have actual/beneficial ownership of one per cent or more securities of the subject company, at the end of the month immediately preceding the date of publication of the research report or date of the public appearance.",
        "The Research Analyst or its associates or relatives do not have any other material conflict of interest at the time of publication of the research report or at the time of public appearance.",
        "The Research Analyst or its associates have not received any compensation from the subject company in the past twelve months.",
        "The Research Analyst or its associates have not managed or co-managed public offering of securities for the subject company in the past twelve months.",
        "The Research Analyst or its associates have not received any compensation for investment banking or merchant banking or brokerage services from the subject company in the past twelve months.",
        "The subject company was not a client of Research Analyst or its employee or its associates during twelve months preceding the date of distribution of the research report and recommendation services provided.",
        "The Research Analyst or its associates have not received any compensation for products or services other than investment banking or merchant banking or brokerage services from the subject company in the past twelve months.",
        "The Research Analyst or its associates have not received any compensation or other benefits from the subject company or third party in connection with the research report.",
        "The Research Analyst has not been engaged in market making activity for the subject company.",
        "The Research Analyst has not served as an officer, director or employee of the subject company.",
        "The Research Analyst did not receive any compensation or other benefits from the companies mentioned in the documents or third party in connection with preparation of the research documents. Accordingly, Research Analyst does not have any material conflict of interest at the time of publication of the research documents.",
    ]),
    ("Disclaimer:", [
        "Investments in securities market are subject to market risks. Read all the related documents carefully before investing.",
        "Registration granted by SEBI, membership from BASL and certification from NISM in no way guarantee performance of the intermediary or provide any assurance of returns to investors.",
        "The fees are paid for Research Report or Research recommendations and are not refundable or cancellable under any circumstances.",
        "Images if any, shared with you are for illustration purposes only.",
        "We are not responsible for any financial loss or any other loss incurred by the client.",
        "Please be fully informed about the risk and costs involved in trading and investing. Please consult your investment advisor before trading. Trade only as per your risk appetite and risk profile.",
        "Trading/investing in stock market is risky due to its volatile nature. Upon accepting our service, you hereby accept that you fully understand the risks involved in trading/investing.",
        "We advise the viewers to apply own discretion while referring testimonials shared by the client. Past performances and results are no guarantee of future performance.",
        "All Report or recommendations shared are confidential and for the reference of paid members only. Any unapproved distribution of sensitive data will be considered as a breach of confidentiality and appropriate legal action shall be initiated.",
        "The Research Report or recommendations must not be used as a singular basis of any investment decision. The views do not consider the risk appetite or the particular circumstances of an individual investor; readers are requested to take professional advice before investing and trading. Our recommendations should not be construed as investment advice.",
        "No representation is made as to the accuracy, completeness, reasonableness, or sufficiency of the information contained in this material, or, in the case of projections, as to their attainability or the assumptions on which they are based. Prospective investors are expected to conduct their own independent due diligence.",
        "This material has been compiled by the Research Analyst based on publicly available information and sources considered reliable; however, such information has not been independently verified. Accordingly, Research Analyst or its associates or relatives shall not be liable for any direct or indirect loss arising from the use of or reliance on this material, and any such liability is expressly disclaimed.",
        "In case of any query, please email on research@niveshaay.com, our team will get back to you and resolve your query. Please state your registered phone number while mailing us.",
        "Reports based on technical and derivative analysis center on studying charts of a stock's price movement, outstanding positions and trading volume, as opposed to focusing on a company's fundamentals and, as such, may not match with a report on a company's fundamentals.",
    ]),
]


def _disclosure_flowables() -> list:
    flow = []
    for heading, body in _DISCLOSURE_SECTIONS:
        if heading:
            flow.append(Paragraph(heading, _footer_subheading_style))
            for n, item in enumerate(body, start=1):
                flow.append(Paragraph(f"{n}.  {item}", _footer_numbered_style))
        else:
            flow.append(Paragraph(body, _footer_body_style))
            flow.append(Spacer(1, 6))
    return flow


_FIRM_ADDRESS_BLOCK = """<b>NIVESHAAY INVESTMENT ADVISORS PRIVATE LIMITED</b><br/>
Trade Name: NIVESHAAY INVESTMENT ADVISORS PRIVATE LIMITED,<br/>
SEBI Registered Research Analyst Registration No. INH000027830, BSE Enlistment No. 7247<br/>
(Type of Registration- Non-Individual, Validity of Registration- Perpetual)<br/>
Address: Office no. 508, 5th floor, SNS platina opposite shrenik residency, Near OM Sai Row house Vesu Surat Gujarat 395007<br/>
Contact No: 7859870559, Email: research@niveshaay.com<br/>
SEBI regional/local office address - SEBI Bhavan, Western Regional Office, Panchvati 1st Lane, Gulbai Tekra Road, Ahmedabad - 380006, Gujarat"""


def _company_flowables(row: dict, image_dir: Path) -> list:
    flow = [Paragraph(_sanitize(row["securityName"]), _company_style)]

    financial_table = row.get("financialTable")
    if financial_table and financial_table.get("headers"):
        tbl = _financial_table_flowable(financial_table, max_width=16.5 * cm)
        if tbl:
            flow.append(tbl)
            flow.append(Spacer(1, 10))

    image_names = row.get("financialImages") or []
    if image_names:
        flow.extend(_snapshot_image_flowables([image_dir / n for n in image_names], max_width=16.5 * cm))

    op_box = _bullets_box_flowable(row.get("operationalPerformance"), max_width=16.5 * cm)
    if op_box:
        flow.append(Paragraph("Operational Performance:", _section_style))
        flow.append(op_box)
        flow.append(Spacer(1, 8))

    outlook_box = _bullets_box_flowable(row.get("outlook"), max_width=16.5 * cm)
    if outlook_box:
        flow.append(Paragraph("Outlook:", _section_style))
        flow.append(outlook_box)

    return flow


def generate_merged_pdf(basket_label: str, rows: list, output_path: Path, consolidated: bool = False,
                         image_dir: Path | None = None) -> None:
    """`rows` is a list of result_updates.json row dicts (already filtered
    + ordered by the caller) for ONE basket. Writes the merged PDF to
    `output_path`. `consolidated=True` adds a subtitle marking this as the
    final once-everyone's-reported send, per the user's own spec -- same
    layout otherwise, since the format doesn't change, just when it's sent.
    `image_dir` resolves each row's financialImages filenames to real paths
    -- pass the caller's _IMAGE_DIR."""
    doc = SimpleDocTemplate(str(output_path), pagesize=A4,
                             topMargin=2.6 * cm, bottomMargin=2.2 * cm, leftMargin=2.2 * cm, rightMargin=2.2 * cm)
    story = [Paragraph(basket_label, _basket_title_style)]
    if consolidated:
        story.append(Paragraph("Consolidated Result Update", _consolidated_subtitle_style))
    story.append(Spacer(1, 6))

    for i, row in enumerate(rows):
        company_flow = _company_flowables(row, image_dir or Path("."))
        story.append(KeepTogether(company_flow))
        if i < len(rows) - 1:
            story.append(Spacer(1, 16))

    # Fixed SEBI disclosure footer, once, at the very end.
    story.append(PageBreak())
    story.append(Paragraph("Disclosure &amp; Disclaimer", _footer_heading_style))
    story.extend(_disclosure_flowables())

    story.append(Spacer(1, 10))
    footer_table = Table([[Paragraph(_FIRM_ADDRESS_BLOCK, _footer_body_style)]], colWidths=[17 * cm])
    footer_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, colors.black),
        ("INNERPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(footer_table)

    doc.build(story, onFirstPage=_page_decoration, onLaterPages=_page_decoration)
