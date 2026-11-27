"""Export unknowns are source evidence, never provisional database zeros."""
from copy import deepcopy
from datetime import date
from decimal import Decimal as D
from io import BytesIO
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pdfplumber
from openpyxl import load_workbook
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, RequestFactory, override_settings
from accounts.models import BusinessProfile
from . import test_manual as fixtures
from .models import Invoice, InvoiceBatch, InvoiceLineItem, InvoiceExtractionAttempt, InvoiceExtractionReview
from .extraction import _extract_local
from .extraction_services import process_upload as upload_invoice, invoice_snapshot
from .export_evidence import financial_export_evidence
from .exporting import invoice_batch_workbook_response
from .pdf_generator import invoice_pdf_data, generate_invoice_pdf


class ProvisionalExportTests(TestCase):
    setUp = fixtures.ManualInvoiceTests.setUp
    payload = fixtures.ManualInvoiceTests.payload
    create = fixtures.ManualInvoiceTests.create
    finalize = fixtures.ManualInvoiceTests.finalize

    def provisional(self, **changes):
        batch = InvoiceBatch.objects.create(user=self.user)
        invoice = Invoice.objects.create(user=self.user, batch=batch, entry_mode='uploaded', invoice_type='sales', invoice_date=date(2026,11,1), status='review', invoice_number='SYNTHETIC-EXPORT', taxable_amount=100, total_amount=118)
        item = InvoiceLineItem.objects.create(invoice=invoice, description='Synthetic service', quantity=1, rate=100, taxable_amount=100, tax_rate=18)
        data = {'taxable_amount':'100', 'cgst_amount':'9', 'sgst_amount':'9', 'igst_amount':'0', 'total_amount':'118', 'confidence':{}, 'line_items':[{'description':item.description,'quantity':'1','rate':'100','taxable_amount':'100','tax_rate':'18','discount':None,'tax_amount':None,'total_amount':None}]}
        data.update(changes)
        attempt = InvoiceExtractionAttempt.objects.create(user=self.user,business=self.business,invoice=invoice,batch=batch,status='review',result=data,file_sha256='a'*64,original_name='fictional.pdf')
        invoice.refresh_from_db();item.refresh_from_db()
        return invoice, item, attempt

    def workbook(self, invoice):
        return load_workbook(BytesIO(invoice_batch_workbook_response(invoice.batch).content))

    def test_unknown_pdf_values_and_roundoff_are_not_zero(self):
        invoice, item, attempt = self.provisional()
        data = invoice_pdf_data(invoice)
        self.assertIsNone(data['lines'][0]['discount']);self.assertIsNone(data['lines'][0]['tax_amount']);self.assertIsNone(data['lines'][0]['total_amount'])
        self.assertIsNone(data['totals']['other_charges']);self.assertIsNone(data['totals']['rounding_adjustment'])
        with pdfplumber.open(BytesIO(generate_invoice_pdf(invoice))) as pdf:
            text=' '.join(p.extract_text() or '' for p in pdf.pages)
        self.assertIn('UNCONFIRMED',text);self.assertIn('Unknown - review',text)
        attempt.refresh_from_db();self.assertIsNone(attempt.result['line_items'][0]['tax_amount'])

    def test_excel_blanks_comments_and_distinct_missing_unknown(self):
        invoice, item, attempt = self.provisional()
        book=self.workbook(invoice);row=book['Line Items']
        for column in (9,10,12):
            self.assertIsNone(row.cell(2,column).value);self.assertIn('unknown',row.cell(2,column).comment.text)
        self.assertIsNone(book['Invoices'].cell(2,12).value)
        self.assertIn('missing',book['Invoices'].cell(2,12).comment.text)
        self.assertEqual(row.cell(2,13).value,'UNCONFIRMED')

    def test_explicit_zero_is_numeric_even_when_model_placeholder_differs(self):
        invoice, item, attempt=self.provisional()
        data=deepcopy(attempt.result);data['line_items'][0].update(discount='0',tax_amount='0',total_amount='0');data['other_charges']='0';data['rounding_adjustment']='0'
        # A separate immutable attempt supplies new source evidence; do not rewrite the old one.
        InvoiceExtractionAttempt.objects.create(user=self.user,business=self.business,invoice=invoice,status='review',result=data,file_sha256='b'*64,original_name='new.pdf')
        book=self.workbook(invoice)
        for col in (9,10,12):self.assertEqual(book['Line Items'].cell(2,col).value,0)
        self.assertEqual(book['Invoices'].cell(2,12).value,0);self.assertEqual(book['Invoices'].cell(2,21).value,0)

    def test_conflicting_total_is_blank_with_raw_candidate_and_pdf_warning(self):
        invoice,item,attempt=self.provisional(confidence={'total_amount':{'level':'conflicting','reason':'Conflicting totals'}})
        book=self.workbook(invoice)
        self.assertIsNone(book['Invoices'].cell(2,13).value);self.assertIn('118',book['Invoices'].cell(2,13).comment.text)
        data=invoice_pdf_data(invoice);self.assertIsNone(data['totals']['total_amount'])
        with pdfplumber.open(BytesIO(generate_invoice_pdf(invoice))) as pdf:text=' '.join(p.extract_text() or '' for p in pdf.pages)
        self.assertIn('Conflicting - review: 118',text)

    def test_missing_source_attempt_does_not_promote_model_zeros(self):
        invoice=self.create();invoice.entry_mode='uploaded';invoice.status='review';invoice.save()
        self.assertIsNone(invoice_pdf_data(invoice)['lines'][0]['tax_amount'])

    def test_confirmed_review_and_verified_saved_numbers_remain_numeric(self):
        invoice,item,attempt=self.provisional()
        InvoiceExtractionReview.objects.create(user=self.user,business=self.business,invoice=invoice,attempt=attempt,snapshot={'invoice':invoice_snapshot(invoice)})
        self.assertIsNone(financial_export_evidence(invoice,[item]))
        self.assertEqual(self.workbook(invoice)['Line Items'].cell(2,9).value,0)
        invoice.status='verified';invoice.save()
        self.assertNotIn('financial_evidence',invoice_pdf_data(invoice))

    def test_stale_review_does_not_authorize_placeholder_values(self):
        invoice,item,attempt=self.provisional()
        InvoiceExtractionReview.objects.create(user=self.user,business=self.business,invoice=invoice,attempt=attempt,snapshot={'invoice':invoice_snapshot(invoice)})
        item.description='Changed after review';item.save()
        self.assertIsNone(self.workbook(invoice)['Line Items'].cell(2,9).value)

    def test_manual_and_verified_exports_preserve_saved_precision(self):
        invoice=self.create(**{'items-0-discount':'0'});before=invoice_pdf_data(invoice)
        self.assertNotIn('financial_evidence',before)
        invoice.entry_mode='uploaded';invoice.status='verified';invoice.save()
        self.assertEqual(invoice_pdf_data(invoice)['totals'],before['totals'])

    def test_percent_discount_is_not_exported_as_money(self):
        invoice,item,attempt=self.provisional()
        data=deepcopy(attempt.result);data['line_items'][0].update(discount='5',discount_type='percent')
        InvoiceExtractionAttempt.objects.create(user=self.user,business=self.business,invoice=invoice,status='review',result=data,file_sha256='c'*64,original_name='percent.pdf')
        cell=self.workbook(invoice)['Line Items'].cell(2,12)
        self.assertIsNone(cell.value);self.assertIn('percentage',cell.comment.text)

    def test_all_twelve_exact_documents_and_81_provisional_lines(self):
        cases=json.loads((settings.BASE_DIR/'sample_data/feature_acceptance/manual_entry/invoice_cases.json').read_text())
        results=[];count=0
        with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            request=RequestFactory().post('/isolated-export-test/');request.user=self.user
            for case in cases:
                path=settings.BASE_DIR/'sample_data/feature_acceptance/documents'/Path(case['source']).name if 'source' in case else settings.BASE_DIR/'sample_data/feature_acceptance/documents'/(case['case_id']+'.pdf')
                if not path.exists():path=settings.BASE_DIR/'sample_data/feature_acceptance/documents'/(case['case_id']+'.pdf')
                batch=InvoiceBatch.objects.create(user=self.user)
                invoice,_=upload_invoice(request=request,business=self.business,batch=batch,invoice_type=case['invoice_type'],upload=SimpleUploadedFile(path.name,path.read_bytes(),content_type='application/pdf'),extractor=_extract_local)
                attempt=invoice.extraction_attempts.first();original=deepcopy(attempt.result)
                dto=invoice_pdf_data(invoice);book=self.workbook(invoice)
                with pdfplumber.open(BytesIO(generate_invoice_pdf(invoice))) as pdf:
                    text=' '.join(p.extract_text() or '' for p in pdf.pages)
                self.assertIn('UNCONFIRMED',text)
                evidence_root=getattr(settings,'EXPORT_ACCEPTANCE_EVIDENCE',None)
                if evidence_root and case['case_id']=='EXCLUSIVE-18':
                    (Path(evidence_root)/'after-provisional-exclusive.pdf').write_bytes(generate_invoice_pdf(invoice))
                    (Path(evidence_root)/'after-provisional-exclusive.xlsx').write_bytes(invoice_batch_workbook_response(invoice.batch).content)
                for index,line in enumerate(dto['lines'],2):
                    count+=1
                    for field,column in [('discount',12),('tax_amount',9),('total_amount',10)]:
                        self.assertIsNone(line[field]);self.assertIsNone(book['Line Items'].cell(index,column).value)
                        self.assertIsNotNone(book['Line Items'].cell(index,column).comment)
                    results.append({'case':case['case_id'],'row':index-1,'pdf_unknown_cells':3,'excel_blank_cells':3,'result':'PASS'})
                attempt.refresh_from_db();self.assertEqual(attempt.result,original)
        self.assertEqual(count,81)
        root=getattr(settings,'EXPORT_ACCEPTANCE_EVIDENCE',None)
        if root:
            with (Path(root)/'all-81-provisional-lines.csv').open('w',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=list(results[0]));writer.writeheader();writer.writerows(results)

    def test_nonfinite_and_unsupported_precision_remain_review_evidence(self):
        invoice,item,attempt=self.provisional()
        data=deepcopy(attempt.result);data['line_items'][0].update(tax_amount='NaN',rate='1.234')
        InvoiceExtractionAttempt.objects.create(user=self.user,business=self.business,invoice=invoice,status='review',result=data,file_sha256='d'*64,original_name='invalid-precision.pdf')
        book=self.workbook(invoice)
        for col in (6,9):
            self.assertIsNone(book['Line Items'].cell(2,col).value)
            self.assertIn('conflicting',book['Line Items'].cell(2,col).comment.text)
        self.assertTrue(generate_invoice_pdf(invoice).startswith(b'%PDF-'))
