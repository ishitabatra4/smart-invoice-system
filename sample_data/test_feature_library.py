"""Fixture-only checks: no Django setup, model imports, database access or email."""
import csv
from datetime import date
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import pdfplumber
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from sample_data import generate_feature_library as library
from invoices.extraction import parse_document
from taxation.reconciliation import parse_uploaded_file
from taxation.tax_rules import estimate_income


class FeatureLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)/'fixtures';cls.manifest=library.generate(cls.root)
        cls.cases={case['case_id']:case for case in library.cases()}
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def parse_pdf(self,path):
        with pdfplumber.open(self.root/path) as pdf:
            pages=[{'page':i+1,'text':page.extract_text() or '', 'method':'embedded','tables':[]} for i,page in enumerate(pdf.pages)]
        return parse_document(pages,'pdf')
    def test_manifest_and_no_overwrite(self):
        self.assertEqual(library.verify(self.root),41)
        before=(self.root/'manifest.json').read_bytes()
        with self.assertRaises(FileExistsError):library.generate(self.root)
        self.assertEqual((self.root/'manifest.json').read_bytes(),before)
    def test_checksum_and_reserved_email(self):
        for party in library.PARTIES:
            self.assertTrue(library.gstin_evidence(party['gstin'])['checksum_valid']);self.assertTrue(party['email'].endswith('@example.test'))
    def test_golden_arithmetic(self):
        values=self.cases['INCLUSIVE-ROUNDING']['expected'];self.assertEqual(values['taxable_amount'],'1000.03');self.assertEqual(values['total_amount'],'1180.03');self.assertEqual(values['cgst_amount'],'90.00')
        self.assertEqual(self.cases['MIXED-RATES']['expected']['total_amount'],'863.00');self.assertEqual(self.cases['DISCOUNTS']['expected']['total_amount'],'1008.90')
        for case in self.cases.values():
            components=sum(Decimal(case['expected'][key]) for key in ['taxable_amount','cgst_amount','sgst_amount','igst_amount'])+Decimal(case['options'].get('adjustment','0'))
            self.assertEqual(components,Decimal(case['expected']['total_amount']))
            self.assertEqual(sum(map(Decimal,case['expected_treatment_breakdown'].values())),Decimal(case['expected']['taxable_amount']))
    def test_every_text_pdf_matches_declared_headers(self):
        for case in self.cases.values():
            result=self.parse_pdf('documents/'+case['case_id']+'.pdf');self.assertEqual(result['invoice_number'],case['reference_number'])
            for key in library.MONEY:self.assertEqual(Decimal(str(result[key])),Decimal(case['expected'][key]),(case['case_id'],key))
            self.assertGreater(len(result['validation_errors']),0,'Extraction must still require review')
    def test_multpage_a4_and_line_count(self):
        with pdfplumber.open(self.root/'documents/MULTIPAGE.pdf') as pdf:
            self.assertGreaterEqual(len(pdf.pages),2)
            for page in pdf.pages:self.assertAlmostEqual(page.width,595.2756,places=2);self.assertAlmostEqual(page.height,841.8898,places=2)
        self.assertEqual(len(self.parse_pdf('documents/MULTIPAGE.pdf')['line_items']),60)
    def test_negative_fields_require_review(self):
        missing=self.parse_pdf('negative/missing_number.pdf');ambiguous=self.parse_pdf('negative/ambiguous_date.pdf')
        self.assertIsNone(missing['invoice_number']);self.assertEqual(missing['confidence']['invoice_number']['level'],'missing');self.assertIsNone(ambiguous['invoice_date']);self.assertEqual(ambiguous['confidence']['invoice_date']['level'],'conflicting')
        self.assertEqual((self.root/'negative/exact_duplicate.pdf').read_bytes(),(self.root/'documents/EXCLUSIVE-18.pdf').read_bytes())
    def test_scanned_pdf_is_really_image_only(self):
        with pdfplumber.open(self.root/'ocr/multipage_scanned.pdf') as pdf:
            self.assertEqual(len(pdf.pages),2)
            for page in pdf.pages:self.assertFalse(page.extract_text());self.assertTrue(page.images)
        with Image.open(self.root/'ocr/invoice.png') as original,Image.open(self.root/'ocr/rotated.png') as rotated:self.assertEqual(original.size,tuple(reversed(rotated.size)))
    def test_reconciliation_formats_are_equivalent(self):
        parsed=[]
        for suffix in ['json','xlsx']:
            path=self.root/('tax/reconciliation_exact.'+suffix);parsed.append(parse_uploaded_file(SimpleUploadedFile(path.name,path.read_bytes())))
        self.assertEqual(parsed[0],parsed[1]);self.assertEqual(len(parsed[0]),1);self.assertEqual(parsed[0][0]['total_tax'],Decimal('180.00'))
    def test_tax_expected_is_application_rule_not_statutory_claim(self):
        case=json.loads((self.root/'tax/preparation_inputs.json').read_text())['income_estimate'];period=SimpleNamespace(kind='financial_year',start_date=date(2026,4,1),end_date=date(2027,3,31))
        inputs={**case,'resident_confirmed':True,'ordinary_only':True};result=estimate_income(period,inputs,'proprietorship')
        self.assertEqual(result['status'],case['expected_status']);self.assertEqual(result['estimated_tax'],Decimal(case['expected_current_application_tax']))
    def test_reversal_and_pagination_references(self):
        notes=json.loads((self.root/'adjustments/credit_debit_workflows.json').read_text());self.assertLess(notes['steps'][0]['document_date'],notes['steps'][2]['cancellation_date'])
        with (self.root/'ui/pagination_60_manual_references.csv').open() as source:rows=list(csv.DictReader(source))
        self.assertEqual(len(rows),60);self.assertEqual(len({row['case_id'] for row in rows}),60)


if __name__=='__main__':unittest.main()
