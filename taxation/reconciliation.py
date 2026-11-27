import json
import re
from zipfile import BadZipFile, ZipFile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from invoices.manual_calculations import money
from django.utils.dateparse import parse_date


class ReconciliationFileError(ValueError):
    pass


HEADER_ALIASES = {
    "supplier_gstin": {"suppliergstin", "gstinofsupplier", "gstin", "ctin", "gstinnumber"},
    "invoice_number": {"invoicenumber", "invoiceno", "inum", "documentnumber", "docno"},
    "invoice_date": {"invoicedate", "date", "dt", "documentdate"},
    "taxable_value": {"taxablevalue", "taxableamount", "taxableamt", "txval"},
    "igst_amount": {"igst", "igstamount", "iamt", "integratedtax", "integratedtaxamount"},
    "cgst_amount": {"cgst", "cgstamount", "camt", "centraltax", "centraltaxamount"},
    "sgst_amount": {"sgst", "sgstamount", "samt", "stateuttax", "stateuttaxamount"},
    "total_tax": {"totaltax", "taxamount", "totaltaxamount"},
}


def normalize_invoice_number(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _header(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        # Excel serial dates (1900 date system).
        from datetime import timedelta
        return date(1899, 12, 30) + timedelta(days=int(value))
    text = str(value or "").strip()
    parsed = parse_date(text)
    if parsed:
        return parsed
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ReconciliationFileError(f"Invalid invoice date: {text or '(blank)'}")


def _decimal(value, label, default=None):
    if value in (None, "") and default is not None:
        return default
    if isinstance(value, str) and value.strip() in {"-", "—"} and default is not None:
        return default
    try:
        result = money(Decimal(str(value).replace(",", "").strip()))
        if not result.is_finite() or result < 0 or result >= Decimal("1000000000000"):
            raise InvalidOperation
        return result
    except (InvalidOperation, AttributeError, ValidationError):
        raise ReconciliationFileError(f"Invalid numeric value for {label}: {value!r}")


def _canonicalize(row):
    result = {}
    for key, value in row.items():
        normalized = _header(key)
        for canonical, aliases in HEADER_ALIASES.items():
            if normalized in aliases:
                result[canonical] = value
                break
    required = ("supplier_gstin", "invoice_number", "invoice_date", "taxable_value")
    missing = [field for field in required if result.get(field) in (None, "")]
    if missing:
        raise ReconciliationFileError("Missing required columns or values: " + ", ".join(missing))
    gstin = re.sub(r"\s+", "", str(result["supplier_gstin"])).upper()
    number = str(result["invoice_number"]).strip()
    if not gstin or not normalize_invoice_number(number):
        raise ReconciliationFileError("Supplier GSTIN and invoice number must not be blank.")
    if not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]", gstin):
        raise ReconciliationFileError(f"Invalid supplier GSTIN: {gstin}")
    if len(number) > 100:
        raise ReconciliationFileError("Invoice number exceeds the 100 character limit.")
    igst = _decimal(result.get("igst_amount"), "IGST", Decimal("0.00"))
    cgst = _decimal(result.get("cgst_amount"), "CGST", Decimal("0.00"))
    sgst = _decimal(result.get("sgst_amount"), "SGST", Decimal("0.00"))
    total_tax = _decimal(result.get("total_tax"), "total tax", igst + cgst + sgst)
    return {
        "supplier_gstin": gstin,
        "invoice_number": number,
        "invoice_date": _date(result["invoice_date"]),
        "taxable_value": _decimal(result["taxable_value"], "taxable value"),
        "igst_amount": igst,
        "cgst_amount": cgst,
        "sgst_amount": sgst,
        "total_tax": total_tax,
    }


def _flatten_json(data):
    """Read flat records and the usual GSTR-2B data.docdata.b2b shape."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("records"), list):
        return _flatten_json(data["records"])
    root = data.get("data", data)
    if isinstance(root, list):
        return _flatten_json(root)
    if isinstance(root, dict):
        docdata = root.get("docdata", root)
        b2b = docdata.get("b2b") if isinstance(docdata, dict) else None
        if isinstance(b2b, list):
            rows = []
            for supplier in b2b:
                for invoice in supplier.get("inv", []):
                    totals = {"taxable_value": Decimal("0"), "igst_amount": Decimal("0"), "cgst_amount": Decimal("0"), "sgst_amount": Decimal("0")}
                    for item in invoice.get("items", []):
                        detail = item.get("itm_det", item)
                        for target, aliases in (("taxable_value", ("txval", "taxable_value")), ("igst_amount", ("iamt", "igst")), ("cgst_amount", ("camt", "cgst")), ("sgst_amount", ("samt", "sgst"))):
                            for alias in aliases:
                                if alias in detail:
                                    totals[target] += Decimal(str(detail[alias] or 0))
                                    break
                    rows.append({"gstin": supplier.get("ctin"), "inum": invoice.get("inum"), "dt": invoice.get("dt"), **totals})
            return rows
    for key in ("invoices", "b2b", "data"):
        if isinstance(root, dict) and isinstance(root.get(key), list):
            return _flatten_json(root[key])
    return [data]


def parse_uploaded_file(upload, *, reject_duplicates=False):
    suffix = upload.name.lower().rsplit(".", 1)[-1] if "." in upload.name else ""
    if upload.size > 10 * 1024 * 1024:
        raise ReconciliationFileError("GSTR-2B files must be 10 MB or smaller.")
    try:
        if suffix == "json":
            data = json.loads(upload.read().decode("utf-8-sig"))
            raw_rows = _flatten_json(data)
        elif suffix == "xlsx":
            with ZipFile(upload) as archive:
                if sum(entry.file_size for entry in archive.infolist()) > 50 * 1024 * 1024:
                    raise ReconciliationFileError("Workbook expands beyond the supported size limit.")
            upload.seek(0)
            from openpyxl import load_workbook
            workbook = load_workbook(upload, read_only=True, data_only=True)
            raw_rows = []
            for sheet in workbook.worksheets:
                if sheet.max_row and sheet.max_row > 10000 or sheet.max_column and sheet.max_column > 100:
                    workbook.close()
                    raise ReconciliationFileError("Workbook exceeds the supported row or column limit.")
                from itertools import islice
                sheet_rows = list(islice(sheet.iter_rows(values_only=True), 10001))
                if len(sheet_rows) > 10000:
                    workbook.close()
                    raise ReconciliationFileError("Workbook exceeds the supported row limit.")
                header_index = None
                header = None
                for row_index, values in enumerate(sheet_rows):
                    canonical_headers = {_header(value) for value in values if value is not None}
                    recognized = {field for field, aliases in HEADER_ALIASES.items() if canonical_headers & aliases}
                    if {"supplier_gstin", "invoice_number", "invoice_date", "taxable_value"}.issubset(recognized):
                        header_index, header = row_index, values
                        break
                if header is None:
                    continue
                for values in sheet_rows[header_index + 1:]:
                    if any(value not in (None, "") for value in values):
                        raw_rows.append(dict(zip(header, values)))
            workbook.close()
        else:
            raise ReconciliationFileError("Unsupported file type. Upload an .xlsx or .json GSTR-2B file.")
    except ReconciliationFileError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, KeyError, BadZipFile, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ReconciliationFileError("Could not read this file. Check that it is a valid .xlsx or JSON document.") from exc
    if len(raw_rows) > 10000:
        raise ReconciliationFileError("Upload at most 10000 reconciliation records.")
    if not raw_rows:
        raise ReconciliationFileError("No invoice records were found in the uploaded file.")
    records = []
    seen = set()
    for index, row in enumerate(raw_rows, 1):
        try:
            record = _canonicalize(row)
        except (ReconciliationFileError, AttributeError, ValueError, TypeError, OverflowError) as exc:
            raise ReconciliationFileError(f"Record {index}: {exc}") from exc
        key = (record["supplier_gstin"].upper(), normalize_invoice_number(record["invoice_number"]), record["invoice_date"])
        if key in seen:
            if reject_duplicates:
                raise ReconciliationFileError("Duplicate document identities require review; no rows discarded.")
            continue
        seen.add(key)
        records.append(record)
    return records


def amount_tolerance():
    return Decimal(str(getattr(settings, "GST_RECONCILIATION_AMOUNT_TOLERANCE", "1.00")))
