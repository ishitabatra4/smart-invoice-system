from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace

import pdfplumber
from django.test import SimpleTestCase

from invoices.pdf_generator import MAX_LINES, PDFGenerationError, render_report_pdf
from .preparation_exports import pdf_fields, summary_rows


class WorkingPaperPDFSummaryTests(SimpleTestCase):
    def test_large_frozen_summary_renders_without_losing_documents_or_adjustments(self):
        values = dict(taxable='1000.00', cgst='90.00', sgst='90.00', igst='0.00', total='1180.00')
        data = {
            'title': 'Synthetic working summary',
            'period': {'start': '2026-11-01', 'end': '2026-11-30'},
            'gstr1': {'adjustments': [dict(id=i, number=f'CN-{i:03}', values=values,
                                        lines=[{'original_number': f'INV-{i:03}', 'amount': '50.00'}])
                                     for i in range(32)]},
            'included': [dict(id=i, number=f'INV-{i:03}', date='2026-11-01',
                              supplier_name='Synthetic supplier', buyer_name='Synthetic buyer', values=values)
                         for i in range(50)],
            'warnings': [{'severity': 'review', 'message': 'Synthetic sources; not filed.'}],
        }
        original = deepcopy(data)
        paper = SimpleNamespace(data=data, pk=1, revision=1, calculation_version='test',
                                get_kind_display=lambda: 'GSTR-1', kind='gstr1')
        full = summary_rows(paper)
        compact = summary_rows(paper, compact=True)
        self.assertGreater(len(full), MAX_LINES)
        self.assertLess(len(compact), MAX_LINES)
        content = render_report_pdf('Working summary', ['Field', 'Saved value'], compact, 'Not filed.')
        with pdfplumber.open(BytesIO(content)) as pdf:
            self.assertTrue(all(abs(page.width - 595.28) < 1 for page in pdf.pages))
            text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
        for i in range(50):
            self.assertIn(f'INV-{i:03}', text)
        for i in range(32):
            self.assertIn(f'CN-{i:03}', text)
        self.assertIn('1180.00', text)
        self.assertIn('Synthetic sources; not filed.', text)
        self.assertEqual(data, original)
        self.assertEqual(summary_rows(paper), full)

    def test_document_count_limit_is_still_enforced(self):
        rows = list(pdf_fields([{'number': f'INV-{i}'} for i in range(MAX_LINES + 1)]))
        with self.assertRaises(PDFGenerationError):
            render_report_pdf('Bounded', ['Field', 'Value'], rows, 'Not filed.')

    def test_unknown_and_explicit_zero_remain_distinct(self):
        self.assertEqual(list(pdf_fields([{'unknown': None, 'explicit_zero': '0.00'}])),
                         [('[0]', 'unknown: ; explicit_zero: 0.00')])
