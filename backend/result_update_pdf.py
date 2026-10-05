"""Builds the merged result-update PDF (see routers/result_updates.py for
the full context). Per company, in this exact order (matching the real
sample format, ACUTAA_1.DOC):
  company name (bold) -> financial-snapshot image (black border drawn
  explicitly, regardless of whether the source image already has one) ->
  "Operational Performance:" + bold-lead-in bullets -> "Outlook:" +
  bold-lead-in bullets.
Basket name appears once as the document title. The SEBI research-analyst
disclosure footer appears once, at the very end, not per company -- it's
fixed legal boilerplate, not something to repeat per page.
"""
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, PageBreak, KeepTogether,
)
from reportlab.platypus.flowables import Flowable

_GREEN = colors.HexColor("#456232")

_styles = getSampleStyleSheet()
_basket_title_style = ParagraphStyle("BasketTitle", parent=_styles["Title"], fontSize=18, textColor=_GREEN, spaceAfter=4)
_consolidated_subtitle_style = ParagraphStyle("ConsolidatedSubtitle", parent=_styles["Normal"], fontSize=11, textColor=colors.black, alignment=1, spaceAfter=14)
_company_style = ParagraphStyle("Company", parent=_styles["Heading1"], fontSize=15, textColor=colors.black, spaceBefore=10, spaceAfter=8)
_section_style = ParagraphStyle("Section", parent=_styles["Heading2"], fontSize=12, textColor=_GREEN, spaceBefore=10, spaceAfter=6)
_bullet_style = ParagraphStyle("Bullet", parent=_styles["Normal"], fontSize=10, leading=14, spaceAfter=5, leftIndent=12)
_footer_heading_style = ParagraphStyle("FooterHeading", parent=_styles["Heading3"], fontSize=11, textColor=colors.black, spaceBefore=10, spaceAfter=4)
_footer_body_style = ParagraphStyle("FooterBody", parent=_styles["Normal"], fontSize=8, leading=11)


class _BorderedImage(Flowable):
    """An image with an explicit black border drawn around it -- doesn't
    rely on the source image already having one baked in."""
    def __init__(self, img_path: str, max_width: float, border_width: float = 1.5):
        super().__init__()
        self.img_path = img_path
        self.border_width = border_width
        reader_img = Image(img_path)
        iw, ih = reader_img.imageWidth, reader_img.imageHeight
        scale = min(max_width / iw, 1.0)
        self.width = iw * scale
        self.height = ih * scale

    def draw(self):
        self.canv.drawImage(self.img_path, 0, 0, width=self.width, height=self.height, preserveAspectRatio=True)
        self.canv.setStrokeColor(colors.black)
        self.canv.setLineWidth(self.border_width)
        self.canv.rect(0, 0, self.width, self.height)


_DISCLOSURE_FOOTER = """
Disclosure: The particulars given in this Disclosure Document have been prepared in accordance with SEBI (Research Analyst) Regulations, 2014. The purpose of the Document is to provide essential information about the research and recommendation services in a manner to assist and enable the prospective client/client in making an informed decision for engaging in research and recommendation services before investing. For the purpose of this Disclosure Document, Research Analyst is Niveshaay Investment Advisors Private Limited (hereinafter referred as &ldquo;Research Analyst&rdquo;)

Business Activity: Research Analyst is registered with SEBI as Research Analyst with Registration No. INH000027830 &amp; BSE Enlistment No. 7247. The firm got its registration on 04/06/2026 and is engaged in research and recommendation Services. The focus of Research Analyst is to provide research and recommendations services to the clients. Analyst aligns its interests with those of the client and seeks to provide the best suited services.

Terms and conditions: The Research Report or Research Recommendation is issued to registered client. The Research Report /Recommendation is based on fundamental analysis. The Research Report/Recommendation is prepared solely for informational purpose and does not constitute an offer document or solicitation to buy or sell or subscribe for securities or other financial instruments for clients.

Disciplinary History: 1. No penalties / directions have been issued by SEBI under the SEBI Act or Regulations made there under against the Research Analyst relating to Research Analyst services. 2. There are no pending material litigations or legal proceedings, findings of inspections or investigations for which action has been taken or initiated by any regulatory authority against the Research Analyst or its employees.

Disclosures with respect to Research Reports and Research Recommendations Services: 1. The Research Analyst or its associates or relatives may have financial interest in the subject company. 2. The Research Analyst or its associates or relatives, may have actual/beneficial ownership of one per cent or more securities of the subject company, at the end of the month immediately preceding the date of publication of the research report or date of the public appearance. 3. The Research Analyst or its associates or relatives do not have any other material conflict of interest at the time of publication of the research report or at the time of public appearance. 4. The Research Analyst or its associates have not received any compensation from the subject company in the past twelve months. 5. The Research Analyst or its associates have not managed or co-managed public offering of securities for the subject company in the past twelve months. 6. The Research Analyst or its associates have not received any compensation for investment banking or merchant banking or brokerage services from the subject company in the past twelve months. 7. The subject company was not a client of Research Analyst or its employee or its associates during twelve months preceding the date of distribution of the research report and recommendation services provided. 8. The Research Analyst or its associates have not received any compensation for products or services other than investment banking or merchant banking or brokerage services from the subject company in the past twelve months. 9. The Research Analyst or its associates have not received any compensation or other benefits from the subject company or third party in connection with the research report. 10. The Research Analyst has not been engaged in market making activity for the subject company. 11. The Research Analyst has not served as an officer, director or employee of the subject company. 12. The Research Analyst did not receive any compensation or other benefits from the companies mentioned in the documents or third party in connection with preparation of the research documents. Accordingly, Research Analyst does not have any material conflict of interest at the time of publication of the research documents.

Disclaimer: 1. Investments in securities market are subject to market risks. Read all the related documents carefully before investing. 2. Registration granted by SEBI, membership from BASL and certification from NISM in no way guarantee performance of the intermediary or provide any assurance of returns to investors. 3. The fees are paid for Research Report or Research recommendations and are not refundable or cancellable under any circumstances. 4. Images if any, shared with you are for illustration purposes only. 5. We are not responsible for any financial loss or any other loss incurred by the client. 6. Please be fully informed about the risk and costs involved in trading and investing. Please consult your investment advisor before trading. Trade only as per your risk appetite and risk profile. 7. Trading/investing in stock market is risky due to its volatile nature. Upon accepting our service, you hereby accept that you fully understand the risks involved in trading/investing. 8. We advise the viewers to apply own discretion while referring testimonials shared by the client. Past performances and results are no guarantee of future performance. 9. All Report or recommendations shared are confidential and for the reference of paid members only. Any unapproved distribution of sensitive data will be considered as a breach of confidentiality and appropriate legal action shall be initiated. 10. The Research Report or recommendations must not be used as a singular basis of any investment decision. The views do not consider the risk appetite or the particular circumstances of an individual investor; readers are requested to take professional advice before investing and trading. Our recommendations should not be construed as investment advice. 11. No representation is made as to the accuracy, completeness, reasonableness, or sufficiency of the information contained in this material, or, in the case of projections, as to their attainability or the assumptions on which they are based. Prospective investors are expected to conduct their own independent due diligence. 12. This material has been compiled by the Research Analyst based on publicly available information and sources considered reliable; however, such information has not been independently verified. Accordingly, Research Analyst or its associates or relatives shall not be liable for any direct or indirect loss arising from the use of or reliance on this material, and any such liability is expressly disclaimed. 13. In case of any query, please email on research@niveshaay.com, our team will get back to you and resolve your query. Please state your registered phone number while mailing us. 14. Reports based on technical and derivative analysis center on studying charts of a stock's price movement, outstanding positions and trading volume, as opposed to focusing on a company's fundamentals and, as such, may not match with a report on a company's fundamentals.
""".strip()

_FIRM_ADDRESS_BLOCK = """<b>NIVESHAAY INVESTMENT ADVISORS PRIVATE LIMITED</b><br/>
Trade Name: NIVESHAAY INVESTMENT ADVISORS PRIVATE LIMITED,<br/>
SEBI Registered Research Analyst Registration No. INH000027830, BSE Enlistment No. 7247<br/>
(Type of Registration- Non-Individual, Validity of Registration- Perpetual)<br/>
Address: Office no. 508, 5th floor, SNS platina opposite shrenik residency, Near OM Sai Row house Vesu Surat Gujarat 395007<br/>
Contact No: 7859870559, Email: research@niveshaay.com<br/>
SEBI regional/local office address - SEBI Bhavan, Western Regional Office, Panchvati 1st Lane, Gulbai Tekra Road, Ahmedabad - 380006, Gujarat"""


def _company_flowables(row: dict, upload_dir: Path) -> list:
    flow = [Paragraph(row["securityName"], _company_style)]

    snapshot = row.get("snapshotImage")
    if snapshot:
        img_path = upload_dir / snapshot
        if img_path.exists():
            flow.append(_BorderedImage(str(img_path), max_width=16.5 * cm))
            flow.append(Spacer(1, 10))

    if row.get("operationalPerformance"):
        flow.append(Paragraph("Operational Performance:", _section_style))
        for b in row["operationalPerformance"]:
            heading = (b.get("heading") or "").strip()
            text = (b.get("text") or "").strip()
            flow.append(Paragraph(f"&bull; <b>{heading}:</b> {text}", _bullet_style))

    if row.get("outlook"):
        flow.append(Paragraph("Outlook:", _section_style))
        for b in row["outlook"]:
            heading = (b.get("heading") or "").strip()
            text = (b.get("text") or "").strip()
            flow.append(Paragraph(f"&bull; <b>{heading}:</b> {text}", _bullet_style))

    return flow


def generate_merged_pdf(basket_label: str, rows: list, upload_dir: Path, output_path: Path, consolidated: bool = False) -> None:
    """`rows` is a list of result_updates.json row dicts (already filtered
    + ordered by the caller) for ONE basket. Writes the merged PDF to
    `output_path`. `consolidated=True` adds a subtitle marking this as the
    final once-everyone's-reported send, per the user's own spec -- same
    layout otherwise, since the format doesn't change, just when it's sent."""
    doc = SimpleDocTemplate(str(output_path), pagesize=A4,
                             topMargin=2 * cm, bottomMargin=2 * cm, leftMargin=2 * cm, rightMargin=2 * cm)
    story = [Paragraph(basket_label, _basket_title_style)]
    if consolidated:
        story.append(Paragraph("Consolidated Result Update", _consolidated_subtitle_style))
    story.append(Spacer(1, 6))

    for i, row in enumerate(rows):
        company_flow = _company_flowables(row, upload_dir)
        story.append(KeepTogether(company_flow))
        if i < len(rows) - 1:
            story.append(Spacer(1, 16))

    # Fixed SEBI disclosure footer, once, at the very end.
    story.append(PageBreak())
    story.append(Paragraph("Disclosure &amp; Disclaimer", _footer_heading_style))
    for para in _DISCLOSURE_FOOTER.split("\n\n"):
        story.append(Paragraph(para, _footer_body_style))
        story.append(Spacer(1, 6))

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

    doc.build(story)
