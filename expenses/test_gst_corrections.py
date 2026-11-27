import csv
from datetime import date
from decimal import Decimal
from io import BytesIO, StringIO

import pdfplumber
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from invoices.models import Invoice, InvoicePayment
from . import tests as fixtures
from .models import ExpenseDetail, RecurringExpense
from .recurrence import generate_due_expenses
from .reporting import CLASSIFICATIONS, export_report, report_rows, saved_classifications

D = Decimal


class GSTCorrectionTests(TestCase):
    setUp = fixtures.ExpenseWorkflowTests.setUp
    expense_payload = fixtures.ExpenseWorkflowTests.expense_payload
    create_expense = fixtures.ExpenseWorkflowTests.create_expense

    def generate(self, inclusive=True, amount="1180.03"):
        today = date.today()
        RecurringExpense.objects.create(
            business=self.business, title="Inclusive subscription", category=self.category,
            supplier=self.supplier, taxable_amount=D(amount), gst_rate=D("18"),
            gst_inclusive=inclusive, supplier_state_code="27", place_of_supply="27",
            supply_category="standard", tax_treatment="taxable", frequency="yearly",
            start_date=today, next_due_date=today, schedule_day=today.day,
        )
        return generate_due_expenses(self.business, today)[0]

    def edit_only_notes(self, invoice):
        financial_fields = ("taxable_amount", "cgst_amount", "sgst_amount", "igst_amount", "total_amount", "pricing_mode")
        before = {key: getattr(invoice, key) for key in financial_fields}
        before_lines = list(invoice.line_items.values("rate", "taxable_amount", "tax_amount", "total_amount", "cgst_amount", "sgst_amount", "igst_amount"))
        url = reverse("expense_edit", args=[invoice.pk])
        form = self.client.get(url).context["form"]
        data = {name: form[name].value() if form[name].value() is not None else "" for name in form.fields if name != "attachment"}
        data["notes"] = "Updated notes only"
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302, str(response.context["form"].errors) if response.context else "")
        invoice.refresh_from_db()
        self.assertEqual({key: getattr(invoice, key) for key in financial_fields}, before)
        self.assertEqual(list(invoice.line_items.values("rate", "taxable_amount", "tax_amount", "total_amount", "cgst_amount", "sgst_amount", "igst_amount")), before_lines)
        self.assertEqual(invoice.expense_detail.notes, "Updated notes only")

    def test_generated_inclusive_flag_and_1180_03_notes_edit_preserves_amounts(self):
        invoice = self.generate()
        self.assertTrue(invoice.expense_detail.gst_inclusive)
        self.assertEqual(invoice.pricing_mode, "inclusive")
        self.assertEqual((invoice.taxable_amount, invoice.total_tax, invoice.total_amount), (D("1000.03"), D("180.00"), D("1180.03")))
        self.edit_only_notes(invoice)

    def test_existing_versioned_inclusive_invoice_overrides_false_detail_flag(self):
        invoice = self.generate()
        ExpenseDetail.objects.filter(invoice=invoice).update(gst_inclusive=False)
        form = self.client.get(reverse("expense_edit", args=[invoice.pk])).context["form"]
        self.assertEqual(form.initial["gst_inclusive"], "inclusive")
        self.assertEqual(form.initial["amount"], D("1180.03"))
        self.edit_only_notes(invoice)
        invoice.expense_detail.refresh_from_db()
        self.assertTrue(invoice.expense_detail.gst_inclusive)

    def test_versioned_exclusive_invoice_overrides_true_detail_flag(self):
        invoice = self.generate(inclusive=False, amount="1000.03")
        self.assertFalse(invoice.expense_detail.gst_inclusive)
        ExpenseDetail.objects.filter(invoice=invoice).update(gst_inclusive=True)
        form = self.client.get(reverse("expense_edit", args=[invoice.pk])).context["form"]
        self.assertEqual(form.initial["gst_inclusive"], "exclusive")
        self.assertEqual(form.initial["amount"], D("1000.03"))
        self.edit_only_notes(invoice)

    def test_ordinary_inclusive_discounted_expense_notes_edit(self):
        invoice = self.create_expense(amount="1200.03", gst_inclusive="inclusive", discount="20")
        self.edit_only_notes(invoice)

    def test_historical_unversioned_form_preserves_existing_detail_basis(self):
        invoice = self.create_expense(amount="1180.03", gst_inclusive="inclusive")
        Invoice.objects.filter(pk=invoice.pk).update(calculation_version=None, pricing_mode=None)
        before = Invoice.objects.filter(pk=invoice.pk).values().get()
        form = self.client.get(reverse("expense_edit", args=[invoice.pk])).context["form"]
        self.assertEqual(form.initial["gst_inclusive"], "inclusive")
        self.assertEqual(form.initial["amount"], D("1180.03"))
        self.assertEqual(Invoice.objects.filter(pk=invoice.pk).values().get(), before)

    def classified_expenses(self):
        invoices = []
        for treatment, _ in CLASSIFICATIONS[:-1]:
            invoices.append(self.create_expense(reference_number=treatment, amount="100", gst_rate="0", tax_treatment=treatment))
        historical = self.create_expense(reference_number="historical", amount="100", gst_rate="0")
        Invoice.objects.filter(pk=historical.pk).update(calculation_version=None, treatment_breakdown=None)
        invoices.append(historical)
        return invoices

    def test_saved_classifications_and_unclassified_history(self):
        self.classified_expenses()
        form, rows, totals = report_rows(self.user, self.business, {"report_type": "complete"})
        self.assertTrue(form.is_valid())
        self.assertEqual(totals["taxable"], D("500"))
        self.assertEqual(totals["classifications"], {key: D("100") for key, _ in CLASSIFICATIONS})
        for row in rows:
            expected = "unclassified" if row["number"] == "historical" else row["number"]
            self.assertEqual(row["classifications"][expected], D("100"))
            self.assertEqual(sum(row["classifications"].values()), D("100"))

    def test_mixed_saved_breakdown_is_not_inferred_from_line_rate(self):
        invoice = self.create_expense(amount="100", gst_rate="0")
        invoice.treatment_breakdown = {"taxable": "20", "exempt": "30", "nil_rated": "10", "non_gst": "40"}
        invoice.save(update_fields=["treatment_breakdown"])
        _, rows, _ = report_rows(self.user, self.business, {})
        self.assertEqual(rows[0]["classifications"], {"taxable": D("20"), "exempt": D("30"), "nil_rated": D("10"), "non_gst": D("40"), "unclassified": D("0")})

    def test_missing_partial_and_invalid_breakdowns_stay_unclassified(self):
        invoice = self.create_expense(amount="100", gst_rate="0")
        for breakdown in (None, {}, {"taxable": "NaN"}, {"taxable": "-1"}, {"taxable": "101"}, {"taxable": "invalid"}):
            with self.subTest(breakdown=breakdown):
                invoice.treatment_breakdown = breakdown
                values = saved_classifications(invoice)
                self.assertEqual(values["unclassified"], D("100"))
                self.assertEqual(values["taxable"], D("0"))
        invoice.treatment_breakdown = {"exempt": "60"}
        self.assertEqual(saved_classifications(invoice)["unclassified"], D("40"))

    def test_csv_xlsx_and_pdf_expose_classifications(self):
        self.classified_expenses()
        _, rows, totals = report_rows(self.user, self.business, {})
        csv_rows = list(csv.reader(StringIO(export_report(rows, "complete", totals, "csv").content.decode())))
        headers = csv_rows[0]
        self.assertEqual(headers[4], "Value before GST")
        for source in csv_rows[1:6]:
            row = dict(zip(headers, source))
            label = "Unclassified" if row["Expense number"] == "historical" else dict(CLASSIFICATIONS)[row["Expense number"]]
            self.assertEqual(row[label], "INR 100.00")
            self.assertEqual(row["Value before GST"], "INR 100.00")
        workbook = load_workbook(BytesIO(export_report(rows, "complete", totals, "xlsx").content))
        self.assertEqual(workbook.active["E1"].value, "Value before GST")
        classifications = list(workbook["Tax classifications"].values)
        self.assertEqual(classifications[0], ("Tax classification", "Value before GST"))
        self.assertEqual(classifications[1:], [(label, 100) for _, label in CLASSIFICATIONS])
        for source in list(workbook.active.values)[1:6]:
            row = dict(zip(headers, source))
            label = "Unclassified" if row["Expense number"] == "historical" else dict(CLASSIFICATIONS)[row["Expense number"]]
            self.assertEqual(row[label], 100)
        with pdfplumber.open(BytesIO(export_report(rows, "complete", totals, "pdf").content)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        self.assertIn("Value before GST", text)
        self.assertIn("Unclassified: INR 100.00", text)
        self.assertIn("Non-GST: INR 100.00", text)

    def test_payment_report_classification_totals_count_each_invoice_once(self):
        invoice = self.create_expense(amount="100", gst_rate="0", tax_treatment="exempt")
        for reference in ("first", "second"):
            InvoicePayment.objects.create(invoice=invoice, user=self.user, party=self.supplier,
                payment_date=date.today(), amount=D("10"), payment_mode="cash", reference_id=reference)
        _, rows, totals = report_rows(self.user, self.business, {"report_type": "payments"})
        self.assertEqual(len(rows), 2)
        self.assertEqual(totals["classifications"]["exempt"], D("100"))
        self.assertEqual(totals["payment_amount"], D("20"))
        for fmt in ("csv", "xlsx"):
            self.assertEqual(export_report(rows, "payments", totals, fmt).status_code, 200)

    def test_report_ui_and_group_exports_use_value_before_gst(self):
        self.classified_expenses()
        response = self.client.get(reverse("expense_reports"), {"report_type": "category"})
        self.assertContains(response, "Value before GST")
        self.assertContains(response, "Unclassified")
        self.assertNotContains(response, "<th>Taxable</th>")
        _, rows, totals = report_rows(self.user, self.business, {"report_type": "category"})
        workbook = load_workbook(BytesIO(export_report(rows, "category", totals, "xlsx").content))
        self.assertEqual(workbook["Summary"]["C1"].value, "Value before GST")
        before = list(Invoice.objects.values())
        export_report(rows, "category", totals, "csv")
        self.assertEqual(list(Invoice.objects.values()), before)
