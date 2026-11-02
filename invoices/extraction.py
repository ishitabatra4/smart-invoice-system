import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

import pdfplumber


ZERO = Decimal("0.00")
HUNDRED = Decimal("100")


def money(value):
    """Round a monetary value to two decimal places."""
    return Decimal(value).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def extract_invoice_data(file_path):
    """Extract invoice fields from a text-based PDF and validate amounts."""

    with pdfplumber.open(file_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    data = {
        "extracted_text": text,
        "invoice_number": "",
        "invoice_date": None,
        "supplier_name": "",
        "supplier_gstin": "",
        "buyer_name": "",
        "buyer_gstin": "",
        "taxable_amount": ZERO,
        "cgst_amount": ZERO,
        "sgst_amount": ZERO,
        "igst_amount": ZERO,
        "other_charges": ZERO,
        "total_amount": ZERO,
        "line_items": [],
        "validation_errors": [],
    }

    errors = data["validation_errors"]

    # Extract non-empty text lines.
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # Supplier name.
    for index, line in enumerate(lines):
        if line.upper() == "TAX INVOICE" and index + 1 < len(lines):
            data["supplier_name"] = lines[index + 1]
            break

    # Invoice number.
    match = re.search(
        r"Invoice Number\s+([A-Z0-9][A-Z0-9/-]*)",
        text,
        re.IGNORECASE,
    )
    if match:
        data["invoice_number"] = match.group(1).strip()

    # Invoice date.
    match = re.search(
        r"Invoice Date\s+(\d{2}/\d{2}/\d{4})",
        text,
        re.IGNORECASE,
    )
    if match:
        try:
            data["invoice_date"] = datetime.strptime(
                match.group(1), "%d/%m/%Y"
            ).date()
        except ValueError:
            errors.append("Invoice date is invalid.")

    # GSTINs: first is supplier, second is buyer.
    gstins = re.findall(
        r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b",
        text.upper(),
    )

    if gstins:
        data["supplier_gstin"] = gstins[0]

    if len(gstins) > 1:
        data["buyer_gstin"] = gstins[1]

    # Buyer name.
    match = re.search(
        r"Bill To\s*\n([^\n]+)",
        text,
        re.IGNORECASE,
    )
    if match:
        data["buyer_name"] = match.group(1).strip()

    # Extract invoice amounts and track fields that were actually found.
    amount_patterns = {
        "taxable_amount": r"Taxable Amount\s+[₹$n]?\s*([\d,]+\.\d{2})",
        "cgst_amount": r"CGST\s+[₹$n]?\s*([\d,]+\.\d{2})",
        "sgst_amount": r"SGST\s+[₹$n]?\s*([\d,]+\.\d{2})",
        "igst_amount": r"IGST\s+[₹$n]?\s*([\d,]+\.\d{2})",
        "other_charges": r"Other Charges\s+[₹$n]?\s*([\d,]+\.\d{2})",
        "total_amount": r"Grand Total\s+[₹$n]?\s*([\d,]+\.\d{2})",
    }

    extracted_amount_fields = set()

    for field, pattern in amount_patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            data[field] = money(match.group(1).replace(",", ""))
            extracted_amount_fields.add(field)

    # Line-item extraction for rows matching the supported PDF layout.
    item_pattern = re.compile(
        r"^\s*\d+\s+(.+?)\s+(\d{4,8})\s+"
        r"([\d.]+)\s+([\d,]+\.\d{2})\s+"
        r"([\d,]+\.\d{2})\s+([\d.]+)%\s*$"
    )

    for line in lines:
        match = item_pattern.match(line)

        if not match:
            continue

        description, hsn, qty, rate, taxable, tax_rate = match.groups()

        try:
            quantity = Decimal(qty)
            rate_value = money(rate.replace(",", ""))
            taxable_value = money(taxable.replace(",", ""))
            tax_rate_value = Decimal(tax_rate)

            if quantity <= 0:
                errors.append(
                    f"Line item '{description.strip()}' has an invalid quantity."
                )
                continue

            if rate_value < 0 or taxable_value < 0 or tax_rate_value < 0:
                errors.append(
                    f"Line item '{description.strip()}' contains a negative amount."
                )
                continue

            calculated_taxable = money(quantity * rate_value)
            calculated_tax = money(
                taxable_value * tax_rate_value / HUNDRED
            )

            data["line_items"].append({
                "description": description.strip(),
                "hsn_sac_code": hsn,
                "quantity": quantity,
                "rate": rate_value,
                "tax_rate": tax_rate_value,
                "taxable_amount": taxable_value,
                "tax_amount": calculated_tax,
                "total_amount": money(taxable_value + calculated_tax),
            })

            if calculated_taxable != taxable_value:
                errors.append(
                    f"Taxable amount for '{description.strip()}' "
                    "does not match quantity multiplied by rate."
                )

        except Exception:
            errors.append(
                f"Could not calculate amounts for line item '{description.strip()}'."
            )

    # Required header fields.
    if not data["invoice_number"]:
        errors.append("Invoice number could not be extracted.")

    if not data["invoice_date"]:
        errors.append("Invoice date could not be extracted.")

    if not data["supplier_name"]:
        errors.append("Supplier name could not be extracted.")

    # Missing amounts must not silently be treated as confirmed zeros.
    required_amount_fields = {
        "taxable_amount": "Taxable amount",
        "cgst_amount": "CGST amount",
        "sgst_amount": "SGST amount",
        "igst_amount": "IGST amount",
        "other_charges": "Other charges",
        "total_amount": "Grand total",
    }

    for field, label in required_amount_fields.items():
        if field not in extracted_amount_fields:
            errors.append(f"{label} could not be extracted.")

    # Validate invoice grand total only when all required amounts were found.
    if required_amount_fields.keys() <= extracted_amount_fields:
        expected_total = money(
            data["taxable_amount"]
            + data["cgst_amount"]
            + data["sgst_amount"]
            + data["igst_amount"]
            + data["other_charges"]
        )

        if data["total_amount"] != expected_total:
            errors.append(
                "Invoice total does not match the sum of extracted amounts."
            )

    # Validate line items against invoice header.
    if data["line_items"]:
        items_taxable = money(sum(
            (item["taxable_amount"] for item in data["line_items"]),
            ZERO,
        ))

        items_tax = money(sum(
            (item["tax_amount"] for item in data["line_items"]),
            ZERO,
        ))

        invoice_tax = money(
            data["cgst_amount"]
            + data["sgst_amount"]
            + data["igst_amount"]
        )

        if (
            "taxable_amount" in extracted_amount_fields
            and items_taxable != data["taxable_amount"]
        ):
            errors.append(
                "Line-item taxable amounts do not match "
                "the invoice taxable amount."
            )

        if (
            {"cgst_amount", "sgst_amount", "igst_amount"}
            <= extracted_amount_fields
            and items_tax != invoice_tax
        ):
            errors.append(
                "Line-item tax amounts do not match the invoice tax amounts."
            )

    return data