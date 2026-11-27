"""Targeted print and warning presentation checks; saved evidence is not rewritten."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from django.test import SimpleTestCase,TestCase
from django.template.loader import render_to_string
from . import test_manual as fixtures
from .pdf_generator import generate_invoice_pdf,invoice_pdf_data
from .models import Invoice
import pdfplumber

class FinalPrintTests(TestCase):
    setUp=fixtures.ManualInvoiceTests.setUp
    payload=fixtures.ManualInvoiceTests.payload
    create=fixtures.ManualInvoiceTests.create
    finalize=fixtures.ManualInvoiceTests.finalize

    def test_short_finalized_invoice_one_a4_page_preserves_saved_content(self):
        invoice=self.create();self.finalize(invoice);invoice=Invoice.objects.prefetch_related('line_items','payments','adjustment_notes').get(pk=invoice.pk)
        before=invoice_pdf_data(invoice);content=generate_invoice_pdf(invoice)
        self.assertEqual(invoice_pdf_data(invoice),before)
        with pdfplumber.open(BytesIO(content)) as pdf:
            self.assertEqual(len(pdf.pages),1);self.assertAlmostEqual(pdf.pages[0].width,595.28,places=1)
            text=pdf.pages[0].extract_text()
            for value in ('224.20','190.00','17.10','Consulting','Current payment summary','Signature:'):self.assertIn(value,text)

    def test_browser_print_has_fixed_columns_and_flowing_long_table(self):
        source=Path('templates/invoices/manual_preview.html').read_text()
        for rule in ('size:A4','table-layout:fixed','display:table-header-group','break-inside:avoid','white-space:nowrap','break-inside:auto'):
            self.assertIn(rule,source)
        self.assertNotIn('overflow-wrap:anywhere}body',source)
        self.assertIn('gst-components',Path('templates/invoices/_manual_document.html').read_text())

class WarningPresentationTests(SimpleTestCase):
    def test_long_incomplete_warning_has_visible_blocker_and_full_expandable_evidence(self):
        warning='Line-item financial reconciliation is incomplete: missing '+', '.join('line '+str(i)+' discount' for i in range(80))+'. Compare the original.'
        notices=[warning,'Supplier GSTIN requires correction; local validation failed.'];before=deepcopy(notices)
        html=render_to_string('invoices/extraction_current_notices.html',{'current_notices':notices})
        for text in ('Financial reconciliation incomplete','human review is required','Show full warning','line 79 discount','Supplier GSTIN requires correction'):
            self.assertIn(text,html)
        self.assertEqual(notices,before)

    def test_financial_discrepancy_amounts_and_html_escaping_always_visible(self):
        notice='Invoice total does not reconcile. Extracted total INR 15950.00; calculated component total INR 15750.00; unexplained difference INR 200.00. '+('Review original. '*40)
        html=render_to_string('invoices/_extraction_warnings.html',{'warning_groups':[{'title':'Financial reconciliation','items':[{'message':notice,'summary':notice,'expandable':False}]}]})
        for value in ('15950.00','15750.00','200.00'):self.assertIn(value,html)
        self.assertNotIn('<details>',html)
        from invoices.templatetags.extraction_display import extraction_warnings
        result=extraction_warnings([notice,'Supplier <script>alert(1)</script>'])
        self.assertFalse(result['warning_groups'][0]['items'][0]['expandable'])
        html=render_to_string('invoices/_extraction_warnings.html',result)
        self.assertNotIn('<script>',html);self.assertIn('&lt;script&gt;',html)


class ExtractionWarningSeverityTests(SimpleTestCase):
    def test_critical_severity_label_summary_and_visible_amounts(self):
        from invoices.templatetags.extraction_display import extraction_warnings
        notices=['Invoice total does not reconcile. Extracted total INR 15950.00; unexplained difference INR 200.00.',
                 'Invoice date could not be confirmed from the document.']
        result=extraction_warnings(notices)
        self.assertEqual((result['warning_total'],result['critical_total']),(2,1))
        self.assertEqual([g['severity'] for g in result['warning_groups']],['critical','review'])
        html=render_to_string('invoices/_extraction_warnings.html',result)
        for text in ('2 warnings','1 critical','Critical','Needs review','15950.00','200.00'):
            self.assertIn(text,html)
