"""Bounded, on-demand invoice PDFs from saved records; no GST calculation or writes."""
from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
from html import escape
from io import BytesIO
import json
from pathlib import Path
import re
from django.utils import timezone

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    KeepTogether, LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle,
)

from .ledger import invoice_outstanding, payment_balance_state, payment_total
from .export_evidence import financial_export_evidence

ZERO = Decimal("0.00")
MAX_LINES = 500
MAX_TEXT = 150_000
MAX_PAGES = 40
TREATMENTS = {
    "taxable": "Taxable (0% is not classified as zero-rated)",
    "exempt": "Exempt", "nil_rated": "Nil-rated", "non_gst": "Non-GST",
}
PARTY_FIELDS = ("name", "legal_name", "gstin", "email", "phone", "address_line", "city", "state", "pincode")


class PDFGenerationError(Exception):
    """A controlled generation failure, safe to report without invoice content."""


def indian_number(value):
    """Group a saved two-decimal amount without changing its precision."""
    value = Decimal(value)
    if not value.is_finite() or value != value.quantize(Decimal("0.01")):
        raise PDFGenerationError("Unsupported monetary precision.")
    sign = "-" if value < 0 else ""
    whole, fraction = format(abs(value), ".2f").split(".")
    tail, head = whole[-3:], whole[:-3]
    groups = []
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return sign + ",".join(groups + [tail]) + "." + fraction


def amount_in_words(value):
    value = Decimal(value)
    if not value.is_finite() or value < 0 or value >= Decimal("1000000000000") or value != value.quantize(Decimal("0.01")):
        raise PDFGenerationError("Unsupported amount in words.")
    ones = ("Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen").split()
    tens = "Zero Ten Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()

    def words(number):
        for size, label in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand"), (100, "Hundred")):
            if number >= size:
                quotient, remainder = divmod(number, size)
                return words(quotient) + " " + label + (" " + words(remainder) if remainder else "")
        if number < 20:
            return ones[number]
        quotient, remainder = divmod(number, 10)
        return tens[quotient] + (" " + ones[remainder] if remainder else "")

    rupees = int(value)
    paise = int((value - rupees) * 100)
    return "Rupees " + words(rupees) + (" and " + words(paise) + " Paise" if paise else "") + " Only"


def _text(value):
    value = str(value or "")
    if len(value) > 12_000:
        raise PDFGenerationError("Text exceeds the document limit.")
    return "".join(char for char in value if ord(char) >= 32 or char in "\n\t")


def invoice_pdf_data(invoice):
    """Deterministic presentation data. Never consult mutable profiles or party links."""
    lines = list(invoice.line_items.all()[:MAX_LINES + 1])
    if len(lines) > MAX_LINES:
        raise PDFGenerationError("Invoice exceeds the PDF item limit.")
    business = invoice.business_snapshot if isinstance(invoice.business_snapshot, dict) else {}
    party = invoice.party_snapshot if isinstance(invoice.party_snapshot, dict) else {}
    business = {key: _text(business.get(key)) for key in PARTY_FIELDS}
    party = {key: _text(party.get(key)) for key in PARTY_FIELDS}
    incomplete_snapshots = not business.get("name") or not party.get("name")
    # Invoice header names/GSTINs are saved historical values, unlike directory links.
    seller = business if invoice.invoice_type == "sales" else party
    buyer = party if invoice.invoice_type == "sales" else business
    seller["name"] = seller["name"] or _text(invoice.supplier_name)
    seller["gstin"] = seller["gstin"] or _text(invoice.supplier_gstin)
    buyer["name"] = buyer["name"] or _text(invoice.buyer_name)
    buyer["gstin"] = buyer["gstin"] or _text(invoice.buyer_gstin)
    cancelled = invoice.document_state == "cancelled" or invoice.status == "cancelled"
    draft = not cancelled and (invoice.document_state == "draft" or invoice.status not in {"verified", "paid"})
    title = "SALES INVOICE" if invoice.invoice_type == "sales" else "PURCHASE RECORD" if invoice.invoice_type == "purchase" else "EXPENSE RECORD"
    if (invoice.entry_mode == "manual" and invoice.invoice_type == "sales" and not draft
            and seller["gstin"] and invoice.supply_category == "standard" and lines
            and all(line.tax_treatment == "taxable" for line in lines) and invoice.total_tax > ZERO):
        title = "TAX INVOICE"
    if invoice.entry_mode != "manual":
        title = "INVOICE RECORD COPY"
    evidence = financial_export_evidence(invoice, lines)
    rows, groups = [], defaultdict(lambda: {key: ZERO for key in ("base", "tax", "cgst", "sgst", "igst")})
    for index, line in enumerate(lines):
        row = {key: getattr(line, key) for key in (
            "quantity", "rate", "discount", "tax_rate", "taxable_amount", "tax_amount", "total_amount",
            "invoice_discount_allocated", "cgst_amount", "sgst_amount", "igst_amount",
        )}
        row.update(description=_text(line.description), hsn=_text(line.hsn_sac_code), unit=_text(line.unit), treatment=_text(line.tax_treatment) or "unclassified")
        if evidence:
            row['evidence'] = evidence['lines'][index]
            row.update({key: value['value'] for key, value in row['evidence'].items()})
        rows.append(row)
        if evidence:
            continue
        group = groups[(row["treatment"], line.tax_rate)]
        group["base"] += line.taxable_amount
        group["tax"] += line.tax_amount
        for component in ("cgst", "sgst", "igst"):
            amount = getattr(line, component + "_amount")
            # A missing historical split is unknown, never inferred from today's rules.
            if amount is None or group[component] is None:
                group[component] = None
            else:
                group[component] += amount
    notices = []
    if incomplete_snapshots:
        notices.append("Historical party/address snapshots are incomplete. Only saved invoice details are shown; current directory details have not been substituted.")
    if invoice.entry_mode != "manual":
        notices.append("Generated record copy, not the original supplier document. Refer to the separately retained original where available.")
    if any(row["treatment"] == "unclassified" for row in rows):
        notices.append("Historical tax treatment or per-line tax components may be unclassified; saved amounts are preserved.")
    if not evidence and rows and (sum((row["taxable_amount"] for row in rows), ZERO) != invoice.taxable_amount
                 or sum((row["tax_amount"] for row in rows), ZERO) != invoice.total_tax
                 or sum((row["total_amount"] for row in rows), ZERO) + invoice.other_charges != invoice.total_amount):
        notices.append("Saved line totals and header totals differ. This copy preserves both without recalculation; review the original record.")
    totals = {key: getattr(invoice, key) for key in ("taxable_amount", "cgst_amount", "sgst_amount", "igst_amount", "other_charges", "total_amount", "invoice_discount_amount", "total_discount_amount")}
    totals["line_discounts"] = sum((line.discount for line in lines), ZERO) if lines else None
    if not evidence and invoice.calculation_version == 1 and rows:
        # Reverse only saved reductions for a presentation subtotal; never calculate GST.
        basis = "total_amount" if invoice.pricing_mode == "inclusive" else "taxable_amount"
        totals["subtotal"] = sum((row[basis] + row["discount"] + (row["invoice_discount_allocated"] or ZERO) for row in rows), ZERO)
    else:
        totals["subtotal"] = None
    if evidence:
        totals.update({key: field['value'] for key, field in evidence['header'].items()})
        totals['line_discounts'] = None
        notices.append('UNCONFIRMED extraction evidence. Unknown, missing and conflicting cells require review; numeric model placeholders are not source evidence. No financial recognition is implied.')
    dates=invoice.source_dates
    def date_label(field,state):
        value=dates[field]
        if not value:return 'Unknown - review required' if state=='unknown' else ''
        return value.isoformat()+(' - extracted, unconfirmed' if state=='extracted' else ' - reviewed' if state=='reviewed' else '')
    data = {
        "renderer_version": "1", "number": _text(invoice.invoice_number), "title": title,
        "type": invoice.get_invoice_type_display(), "date": date_label('invoice_date',dates['invoice_state']),
        "due": date_label('due_date',dates['due_state']),
        "state": "CANCELLED" if cancelled else "DRAFT" if draft else "FINALIZED" if invoice.document_state == "finalized" else "VERIFIED RECORD",
        "seller": seller, "buyer": buyer, "supplier_state": _text(invoice.supplier_state_code),
        "place": _text(invoice.place_of_supply), "supply": _text(invoice.supply_category),
        "pricing": _text(invoice.pricing_mode), "lines": rows, "totals": totals,
        "tax_groups": [{"treatment": treatment, "rate": rate, **values} for (treatment, rate), values in sorted(groups.items())],
        "words": amount_in_words(invoice.total_amount) if not evidence else "Unconfirmed extracted total; review the original before financial use.", "notices": notices,
        "payment": {"status": payment_balance_state(invoice), "paid": payment_total(invoice), "outstanding": invoice_outstanding(invoice)},
        "payment_terms": _text(invoice.payment_terms), "notes": _text(invoice.notes), "terms": _text(invoice.terms_conditions),
    }
    if evidence:
        data['financial_evidence'] = evidence
        data['payment'] = {'status': 'unconfirmed - not recognized', 'paid': None, 'outstanding': None}
    if invoice.entry_mode!='manual' and invoice.created_at:
        data['uploaded']=timezone.localtime(invoice.created_at).strftime('%Y-%m-%d %H:%M:%S %Z')
    saved_notes = getattr(invoice, "_prefetched_objects_cache", {}).get("adjustment_notes", [])
    if saved_notes:
        from .adjustment_reporting import adjusted_invoice_amount, overpayment
        data["payment"].update(adjusted_total=adjusted_invoice_amount(invoice), overpayment=overpayment(invoice))
    serialized = json.dumps(data, default=str, sort_keys=True, ensure_ascii=False)
    if len(serialized) > MAX_TEXT:
        raise PDFGenerationError("Invoice exceeds the PDF content limit.")
    data["revision"] = sha256(serialized.encode()).hexdigest()[:16]
    return data


class _BoundedCanvas(Canvas):
    def __init__(self, *args, **kwargs):
        kwargs["invariant"] = 1
        super().__init__(*args, **kwargs)

    def showPage(self):
        if self.getPageNumber() > MAX_PAGES:
            raise PDFGenerationError("Invoice exceeds the PDF page limit.")
        super().showPage()


def register_pdf_fonts():
    font_dir = Path(reportlab.__file__).parent / "fonts"
    for name, filename in (("Invoice", "Vera.ttf"), ("InvoiceBold", "VeraBd.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(font_dir / filename)))


def render_invoice_pdf(data):
    """Render trusted DTO structure with every user-provided text value escaped."""
    register_pdf_fonts()
    normal = ParagraphStyle("normal", fontName="Invoice", fontSize=8, leading=10, textColor=colors.HexColor("#25313d"), splitLongWords=True)
    small = ParagraphStyle("small", parent=normal, fontSize=6.5, leading=8)
    bold = ParagraphStyle("bold", parent=normal, fontName="InvoiceBold")
    subheading = ParagraphStyle("subheading", parent=bold, keepWithNext=True, spaceAfter=4)
    table_heading = ParagraphStyle("table_heading", parent=bold, spaceAfter=4)
    heading = ParagraphStyle("heading", parent=bold, fontSize=17, leading=20, spaceAfter=6)
    right = ParagraphStyle("right", parent=small, alignment=TA_RIGHT)

    def p(value, style=normal):
        return Paragraph(escape(_text(value)).replace("\n", "<br/>"), style)

    def money(value):
        return indian_number(value) if value is not None else "Unknown - review" if data.get("financial_evidence") else "Not recorded"

    def evidenced(value, field=None, quantity=False):
        if field and field['state'] in {'unknown', 'missing', 'conflicting'}:
            label = field['state'].title() + ' - review'
            if field['state'] == 'conflicting' and field['raw']:
                label += ': ' + field['raw']
            return label
        return format(value, 'f') if quantity and value is not None else money(value)

    def section(title, keep_next=True):
        return [Spacer(1, 6), p(title, subheading if keep_next else table_heading)]

    def grid(rows, widths, header=True):
        table = LongTable(rows, colWidths=widths, repeatRows=1 if header else 0, splitInRow=1, hAlign="LEFT")
        commands = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LINEBELOW", (0, 0), (-1, -1), .3, colors.HexColor("#dce1e6"))]
        if header:
            commands.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf0f3")))
        table.setStyle(TableStyle(commands))
        return table

    width = A4[0] - 60
    commercial = data.get("commercial", False)
    adjustment = data.get("adjustment", False)
    story = [p(data["title"], heading), p(data["state"], bold), Spacer(1, 10)]
    if commercial:
        story += [p(data["commercial_notice"], bold), Spacer(1, 10)]
    info = [("Invoice number", data["number"]), ("Invoice date", data["date"]), ("Due date", data["due"]), ("Document type", data["type"]), ("Place of supply (state code)", data["place"]), ("Supplier state code", data["supplier_state"]), ("Supply category", data["supply"]), ("Pricing basis", data["pricing"])]
    if data["supply"] == "reverse_charge":
        info.append(("Reverse charge", "Yes — as recorded"))
    info = [(label, value) for label, value in info if value]
    if data.get('uploaded'):info.append(('Uploaded',data['uploaded']))
    if data.get("financial_evidence"):
        info.append(("Financial evidence", "UNCONFIRMED - review required"))
    if commercial or adjustment:
        labels = {"Invoice number": "Document number", "Invoice date": "Document date", "Due date": "Valid until"}
        info = [(labels.get(label, label), value) for label, value in info]
        if data.get("issued"):
            info.append(("Explicitly issued", data["issued"]))
    info_rows = []
    for offset in range(0, len(info), 2):
        pair = info[offset:offset + 2]
        if len(pair) == 1:
            pair.append(("", ""))
        info_rows.append([p(value, small) for label, value in pair for value in (label, value)])
    story.append(grid(info_rows, [width * .23, width * .27] * 2, header=False))

    def party_text(party, label):
        values = [label, party["legal_name"] or party["name"] or "Name not recorded"]
        if party["legal_name"] and party["name"] and party["legal_name"] != party["name"]:
            values.append(party["name"])
        values += [party["address_line"], " ".join(party[key] for key in ("city", "state", "pincode") if party[key])]
        if party["gstin"]:
            values.append("GSTIN: " + party["gstin"])
            if len(party["gstin"]) == 15 and party["gstin"][:2].isdigit():
                values.append("GSTIN state code: " + party["gstin"][:2])
        values += [party["email"], party["phone"]]
        return p("\n".join(value for value in values if value))

    story += [Spacer(1, 9), grid([[party_text(data["seller"], "SELLER / SUPPLIER"), party_text(data["buyer"], "BILL TO / BUYER")]], [width / 2] * 2, header=False)]
    story += section("Items · amounts in INR", keep_next=not (commercial or adjustment))
    headers = ("#", "Description / HSN-SAC", "Qty / unit", "Unit rate", "Line discount", "Value before GST", "GST %", "GST amount", "Line total")
    rows = [[p(value, small) for value in headers]]
    for index, row in enumerate(data["lines"], 1):
        description = row["description"]
        if row["hsn"]:
            description += "\nHSN/SAC: " + row["hsn"]
        if row["treatment"] != "taxable":
            description += "\n" + TREATMENTS.get(row["treatment"], "Unclassified")
        if row["invoice_discount_allocated"]:
            description += "\nInvoice discount allocated: INR " + money(row["invoice_discount_allocated"])
        fields = row.get('evidence', {})
        values = [str(index), description, evidenced(row['quantity'], fields.get('quantity'), True) + '\n' + row['unit']]
        values += [evidenced(row[key], fields.get(key), key == 'tax_rate') for key in ('rate', 'discount', 'taxable_amount', 'tax_rate', 'tax_amount', 'total_amount')]
        rows.append([p(value, small if column < 3 else right) for column, value in enumerate(values)])
    if data["lines"]:
        story.append(grid(rows, [26, 119, 42, 58, 54, 68, 32, 66, width - 465]))
    else:
        story.append(p("No itemized lines recorded. Header amounts below are the saved financial record."))
    if data["tax_groups"]:
        story += section("GST estimates by saved rate and treatment · INR" if commercial else "GST summary by saved rate and treatment · INR")
        rows = [[p(value, small) for value in ("Treatment / rate", "Value before GST", "CGST", "SGST", "IGST", "Total GST")]]
        for group in data["tax_groups"]:
            treatment = "Taxable" if group["treatment"] == "taxable" else TREATMENTS.get(group["treatment"], "Unclassified")
            rows.append([p(treatment + " / " + format(group["rate"], "f") + "%", small)] + [p(money(group[key]), right) for key in ("base", "cgst", "sgst", "igst", "tax")])
        story.append(grid(rows, [135] + [(width - 135) / 5] * 5))
        story.append(p("0% taxable, exempt, nil-rated and non-GST are distinct treatments. No zero-rated supply is inferred.", small))
    totals = data["totals"]
    total_rows = []
    if totals["subtotal"] is not None:
        total_rows += [("Subtotal (" + data["pricing"] + ", before discounts)", totals["subtotal"])]
    for label, key in (("Line-item discounts (already included)", "line_discounts"), ("Invoice discount (already included)", "invoice_discount_amount"), ("Total discounts (already included)", "total_discount_amount"), ("Value before GST, all treatments", "taxable_amount"), ("CGST", "cgst_amount"), ("SGST", "sgst_amount"), ("IGST", "igst_amount"), ("Rounding adjustment" if data["pricing"] else "Other charges / adjustment", "other_charges"), ("GRAND TOTAL", "total_amount")):
        if totals.get(key) is not None or data.get("financial_evidence"):
            if data.get("financial_evidence") and key == "other_charges":
                label = "Other charges (extracted; excludes unknown round-off)"
            total_rows.append((label, evidenced(totals.get(key), data.get("financial_evidence", {}).get("header", {}).get(key))))
    if data.get("financial_evidence"):
        field = data["financial_evidence"]["header"]["rounding_adjustment"]
        total_rows.insert(-1, ("Round-off (extracted)", evidenced(field["value"], field)))
    totals_table = grid([[p(label, bold if label == "GRAND TOTAL" else normal), p("INR " + (value if isinstance(value, str) else money(value)), bold if label == "GRAND TOTAL" else normal)] for label, value in total_rows], [width * .64, width * .36], header=False)
    totals_table.setStyle(TableStyle([("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#edf0f3"))]))
    story.append(KeepTogether(section("Adjustment totals" if adjustment else "Estimated document totals" if commercial else "Invoice totals") + [totals_table]))
    story += [Spacer(1, 10), p("Amount in words", bold), p(data["words"])]
    if not commercial and not adjustment:
        story += section("Current payment summary")
        story.append(p("Live ledger information at download; this is not an archived payment statement at finalization.", small))
        payment = data["payment"]
        story.append(p("Status: " + payment["status"].title() + " · Recorded payments: INR " + money(payment["paid"]) + " · Outstanding: INR " + money(payment["outstanding"])))
        if "adjusted_total" in payment:
            story.append(p("Net adjusted invoice: INR " + money(payment["adjusted_total"]) + " · Overpayment / refund review: INR " + money(payment["overpayment"])))

    if data["state"] in {"DRAFT", "CANCELLED"}:
        story.append(p("This document is not an active payment demand.", small))
    for title, key in (("Payment terms", "payment_terms"), ("Notes", "notes"), ("Terms and conditions", "terms")):
        if data[key]:
            if commercial and key == "payment_terms":
                title = "Commercial terms"
            story += section(title)
            # Bounded chunks permit long free text to flow across pages safely.
            story.extend(p(data[key][offset:offset + 800]) for offset in range(0, len(data[key]), 800))
    for notice in data["notices"]:
        story += [Spacer(1, 8), p(notice, small)]
    if data["state"] not in {"DRAFT", "CANCELLED"} and (data["type"] == "Sales Invoice" or (adjustment and data.get("direction") == "issued")):
        story += [Spacer(1, 10), KeepTogether([p("Authorized signatory", bold), Spacer(1, 16), p("Signature: ____________________", small)])]
    if not commercial and not adjustment:
        story += [Spacer(1, 10), p("Generated by Smart Invoice · No digital signature is asserted.", small)]

    def page(canvas, doc):
        canvas.saveState()
        if data["state"] in {"DRAFT", "CANCELLED"}:
            canvas.setFillColor(colors.HexColor("#e3e6e9"))
            canvas.setFont("InvoiceBold", 60)
            canvas.translate(A4[0] / 2, A4[1] / 2)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, data["state"])
            canvas.restoreState()
            canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#dce1e6"))
        canvas.line(30, 35, A4[0] - 30, 35)
        canvas.setFont("Invoice", 6)
        canvas.setFillColor(colors.HexColor("#566270"))
        canvas.drawString(30, 23, "Smart Invoice · PDF v1 · Copy " + data["revision"])
        canvas.drawRightString(A4[0] - 30, 23, "Page " + str(doc.page))
        canvas.restoreState()

    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=A4, leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=48, title="Invoice " + data["number"], author="Smart Invoice", pageCompression=1)
    document.build(story, onFirstPage=page, onLaterPages=page, canvasmaker=_BoundedCanvas)
    result = output.getvalue()
    if not result.startswith(b"%PDF-") or not result.rstrip().endswith(b"%%EOF") or len(result) > 5 * 1024 * 1024:
        raise PDFGenerationError("Unable to produce a complete bounded PDF.")
    return result


def generate_invoice_pdf(invoice):
    return render_invoice_pdf(invoice_pdf_data(invoice))


def invoice_pdf_filename(invoice):
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", invoice.invoice_number).strip("-_")[:70] or "invoice"
    return stem + "-" + str(invoice.pk) + ".pdf"


def render_report_pdf(title, headers, rows, summary):
    """Bounded A4 tabular exports sharing invoice fonts, escaping and page limits."""
    if len(rows)>MAX_LINES or sum(len(str(v)) for row in rows for v in row)+len(summary)>MAX_TEXT:
        raise PDFGenerationError("Report exceeds the PDF limit; select a shorter period.")
    register_pdf_fonts()
    normal=ParagraphStyle("report",fontName="Invoice",fontSize=7,leading=10,splitLongWords=True)
    heading=ParagraphStyle("report-heading",parent=normal,fontName="InvoiceBold",fontSize=15,leading=20)
    def text(value):return Paragraph(escape(_text(value)).replace("\n","<br/>"),normal)
    table=LongTable([[text(v) for v in row] for row in [headers,*rows]],colWidths=[(A4[0]-60)/len(headers)]*len(headers),repeatRows=1,splitInRow=1)
    table.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#edf0f3")),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
    output=BytesIO();doc=SimpleDocTemplate(output,pagesize=A4,leftMargin=30,rightMargin=30,topMargin=35,bottomMargin=45)
    def footer(canvas,doc):
        canvas.saveState();canvas.setFont("Invoice",7);canvas.drawString(30,23,"Smart Invoice · Preparation estimate · No portal filing");canvas.drawRightString(A4[0]-30,23,"Page "+str(doc.page));canvas.restoreState()
    doc.build([Paragraph(escape(title),heading),Spacer(1,10),text(summary),Spacer(1,12),table],onFirstPage=footer,onLaterPages=footer,canvasmaker=_BoundedCanvas)
    return output.getvalue()
