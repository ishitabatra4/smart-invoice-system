"""Date candidates, required review and provisional storage must remain distinct."""
from datetime import date
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import json
import pdfplumber
from openpyxl import load_workbook
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase,TestCase
from django.urls import reverse
from . import test_extraction as fixtures
from .extraction import parse_document,date_candidates
from .extraction_services import validate_review_dates
from .date_evidence import with_invoice_dates
from .models import Invoice,InvoiceExtractionAttempt as Attempt,InvoiceExtractionReview as Review,InvoicePayment
from .exporting import invoice_batch_workbook_response
from analytics.services import build_analytics_context
from taxation.views import _period_purchase_invoices


def parsed(text):return parse_document([{'page':1,'text':text,'tables':[],'method':'embedded'}],'digital_pdf')


class DateCandidateTests(SimpleTestCase):
    def test_supported_unambiguous_month_name_and_iso_formats(self):
        for text in ('01-Nov-2026','01-November-2026','01 Nov 2026','01 November 2026','2026-11-01','November 1, 2026'):
            with self.subTest(text=text):
                result=parsed('Invoice Date: '+text+'\nDue Date: 30-Nov-2026')
                self.assertEqual(result['invoice_date'],'2026-11-01');self.assertEqual(result['due_date'],'2026-11-30')
                self.assertEqual(result['confidence']['invoice_date']['level'],'high')

    def test_ambiguous_numeric_dates_are_not_guessed(self):
        for text in ('01/11/2026','01-11-2026'):
            result=parsed('Invoice Date: '+text)
            self.assertIsNone(result['invoice_date']);self.assertEqual(result['confidence']['invoice_date']['level'],'conflicting')
            self.assertEqual(set(result['confidence']['invoice_date']['candidates']),{'2026-11-01','2026-01-11'})

    def test_legacy_unambiguous_numeric_formats_and_leap_year(self):
        for raw,expected in [('20/11/2026','2026-11-20'),('20-11-2026','2026-11-20'),('29-Feb-2024','2024-02-29')]:
            self.assertEqual(parsed('Invoice Date: '+raw)['invoice_date'],expected)

    def test_invalid_and_missing_dates_remain_unknown(self):
        for raw in ('31-Nov-2026','29-Feb-2026','2026-02-30','November 32, 2026','not provided'):
            with self.subTest(raw=raw):
                result=parsed('Invoice Date: '+raw+'\nDue Date: '+raw)
                self.assertIsNone(result['invoice_date']);self.assertIsNone(result['due_date'])
                self.assertEqual(result['confidence']['invoice_date']['level'],'low')
        self.assertEqual(parsed('No dates')['confidence']['invoice_date']['level'],'missing')

    def test_conflicting_dates_across_pages_are_retained_as_candidates(self):
        result=parse_document([{'page':i,'text':'Invoice Date: '+text+'\nDue Date: '+text,'tables':[],'method':'embedded'} for i,text in enumerate(('01-Nov-2026','02-Nov-2026'),1)],'digital_pdf')
        for field in ('invoice_date','due_date'):
            self.assertIsNone(result[field]);self.assertEqual(result['confidence'][field]['level'],'conflicting')
            self.assertEqual(set(result['confidence'][field]['candidates']),{'2026-11-01','2026-11-02'})

    def test_repeated_same_date_in_different_formats_is_not_a_conflict(self):
        result=parsed('Invoice Date: 01-Nov-2026\nInvoice Date: November 1, 2026')
        self.assertEqual(result['invoice_date'],'2026-11-01')
        self.assertEqual(result['confidence']['invoice_date']['raw_candidates'],['01-Nov-2026','November 1, 2026'])

    def test_invalid_candidate_does_not_disappear_beside_valid_date(self):
        result=parsed('Invoice Date: 01-Nov-2026\nInvoice Date: 31-Nov-2026')
        self.assertIsNone(result['invoice_date']);self.assertEqual(result['confidence']['invoice_date']['level'],'conflicting')

    def test_po_supply_and_unrelated_dates_do_not_become_invoice_date(self):
        unrelated='Purchase Order Date: 01-Oct-2026\nPO Date: 02-Oct-2026\nDate of Supply: 03-Oct-2026\nDelivery Date: 04-Oct-2026\nDocument Date: 05-Oct-2026\n'
        self.assertIsNone(parsed(unrelated)['invoice_date'])
        result=parsed(unrelated+'Invoice Date: 01-Nov-2026\nDue Date: 30-Nov-2026')
        self.assertEqual(result['invoice_date'],'2026-11-01');self.assertEqual(result['due_date'],'2026-11-30')
        self.assertEqual(parsed('Date: 20 Nov 2026')['invoice_date'],'2026-11-20')

    def test_all_twelve_exact_feature_pdfs(self):
        cases=json.loads((settings.BASE_DIR/'sample_data/feature_acceptance/manual_entry/invoice_cases.json').read_text())
        self.assertEqual(len(cases),12)
        for case in cases:
            with self.subTest(case=case['case_id']):
                with pdfplumber.open(settings.BASE_DIR/'sample_data/feature_acceptance/documents'/ (case['case_id']+'.pdf')) as pdf:
                    pages=[{'page':i,'text':p.extract_text() or '', 'tables':p.extract_tables(),'method':'embedded'} for i,p in enumerate(pdf.pages,1)]
                result=parse_document(pages,'digital_pdf')
                self.assertEqual(result['invoice_date'],case['invoice_date']);self.assertEqual(result['due_date'],'2026-11-30')
                self.assertEqual(result['invoice_date'],'2026-10-01' if case['case_id']=='HISTORICAL-PAYMENT' else '2026-11-01')

    def test_service_requires_valid_reviewed_dates(self):
        for value in (None,'','2026-02-30'):
            with self.assertRaises(ValidationError):validate_review_dates({'invoice_date':value,'due_date':None})
        with self.assertRaises(ValidationError):validate_review_dates({'invoice_date':'2026-11-01','due_date':'2026-10-01'})
        validate_review_dates({'invoice_date':date(2026,11,1),'due_date':None})


class DateEvidenceWorkflowTests(TestCase):
    def setUp(self):fixtures.ExtractionWorkflowTests.setUp(self)
    def upload(self,text=fixtures.TEXT):
        return fixtures.ExtractionWorkflowTests.upload(self,data=parsed(text))
    def payload(self,attempt,**updates):return fixtures.ExtractionWorkflowTests.payload(self,attempt,**updates)

    def test_real_pdf_upload_persists_candidates_without_confirming_due_date(self):
        source=settings.BASE_DIR/'sample_data/feature_acceptance/documents/EXCLUSIVE-18.pdf'
        response=self.client.post(reverse('invoice_upload'),{'invoice_type':'sales','source_files':SimpleUploadedFile('feature.pdf',source.read_bytes(),content_type='application/pdf')})
        self.assertEqual(response.status_code,302);a=Attempt.objects.get()
        self.assertEqual(a.result['invoice_date'],'2026-11-01');self.assertEqual(a.result['due_date'],'2026-11-30')
        self.assertEqual(a.invoice.invoice_date,date(2026,11,1));self.assertIsNone(a.invoice.due_date)
        self.assertEqual(a.invoice.source_dates['invoice_state'],'extracted');self.assertFalse(Review.objects.exists())

    def test_unknown_provisional_date_is_not_displayed_as_storage_date(self):
        _,a=self.upload(fixtures.TEXT.replace('Invoice Date: 20/11/2026\n','').replace('Due Date: 10 Dec 2026\n',''))
        self.assertIsNotNone(a.invoice.invoice_date);self.assertIsNone(a.result['invoice_date'])
        for route,args in [('invoice_list',[]),('invoice_detail',[a.invoice_id]),('extraction_review',[a.pk]),('invoice_batch_detail',[a.batch_id])]:
            page=self.client.get(reverse(route,args=args));self.assertEqual(page.status_code,200)
            self.assertContains(page,'Unknown — review required');self.assertContains(page,'Uploaded:')
        page=self.client.get(reverse('extraction_review',args=[a.pk]));self.assertIsNone(page.context['form'].initial.get('invoice_date'))

    def test_extracted_dates_are_labelled_unconfirmed_and_recent_table_is_safe(self):
        _,a=self.upload()
        for route,args in [('invoice_list',[]),('invoice_detail',[a.invoice_id]),('extraction_review',[a.pk])]:
            page=self.client.get(reverse(route,args=args));self.assertContains(page,'20 Nov 2026');self.assertContains(page,'extracted, unconfirmed');self.assertContains(page,'Uploaded:')
        from django.template.loader import render_to_string
        rendered=render_to_string('invoices/_invoice_date.html',{'invoice':a.invoice})
        self.assertIn('extracted, unconfirmed',rendered)

    def test_reviewed_dates_persist_independently_of_immutable_candidates(self):
        _,a=self.upload();before=deepcopy(a.result)
        response=self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a,invoice_date='2026-11-19',due_date='2026-12-11'))
        self.assertEqual(response.status_code,302);a.refresh_from_db();inv=Invoice.objects.get(pk=a.invoice_id)
        self.assertEqual(inv.invoice_date,date(2026,11,19));self.assertEqual(inv.due_date,date(2026,12,11));self.assertEqual(a.result,before)
        self.assertEqual(inv.source_dates['invoice_state'],'reviewed');self.assertEqual(inv.source_dates['due_state'],'reviewed')
        self.client.post(reverse('invoice_verify',args=[inv.pk]),{'confirm':'on'});inv.refresh_from_db()
        self.assertEqual(inv.status,'verified');self.assertEqual(inv.source_dates['invoice_state'],'saved')
        self.assertContains(self.client.get(reverse('invoice_list')),'19 Nov 2026')

    def test_missing_date_blocks_confirmation_and_unconfirmed_verification(self):
        _,a=self.upload();response=self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a,invoice_date=''))
        self.assertEqual(response.status_code,200);self.assertTrue(response.context['form'].errors.get('invoice_date'));self.assertFalse(Review.objects.exists())
        self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'review')

    def test_clearing_known_due_date_still_requires_explicit_acknowledgement(self):
        _,a=self.upload();data=self.payload(a,due_date='')
        response=self.client.post(reverse('extraction_review',args=[a.pk]),data)
        self.assertEqual(response.status_code,200);self.assertFalse(Review.objects.exists())
        data=self.payload(a,due_date='',clear_due_date_confirmed='on')
        self.assertEqual(self.client.post(reverse('extraction_review',args=[a.pk]),data).status_code,302)
        a.refresh_from_db();self.assertEqual(a.result['due_date'],'2026-12-10');self.assertIsNone(a.invoice.due_date)
        self.assertTrue(Review.objects.get().snapshot['due_date_clear_confirmed'])

    def test_date_queries_and_financial_tax_scopes_ignore_unknown_placeholder(self):
        baseline=build_analytics_context(self.user,date(2026,1,1),date(2026,12,31),'custom')['totals']
        _,a=self.upload(fixtures.TEXT.replace('Invoice Date: 20/11/2026\n',''))
        qs=with_invoice_dates(Invoice.objects.filter(pk=a.invoice_id))
        self.assertFalse(qs.filter(evidence_invoice_date__range=(date(2026,1,1),date(2026,12,31))).exists())
        self.assertEqual(build_analytics_context(self.user,date(2026,1,1),date(2026,12,31),'custom')['totals'],baseline)
        self.assertFalse(_period_purchase_invoices(self.user,date(2026,1,1),date(2026,12,31)).filter(pk=a.invoice_id).exists())
        from invoices.ledger_views import _invoice_rows
        self.assertEqual(_invoice_rows(self.user,[self.vendor]),[]);self.assertFalse(InvoicePayment.objects.exists())

    def test_annotated_list_does_not_query_dates_per_row(self):
        self.upload()
        with self.assertNumQueries(1):
            values=[inv.source_dates for inv in with_invoice_dates(Invoice.objects.filter(user=self.user))]
        self.assertEqual(values[0]['invoice_state'],'extracted')

    def test_working_paper_period_and_source_use_evidence_not_storage_placeholder(self):
        _,a=self.upload(fixtures.TEXT.replace('20/11/2026','20/10/2026'))
        Invoice.objects.filter(pk=a.invoice_id).update(invoice_date=date(2026,11,24))
        from types import SimpleNamespace
        from taxation.preparation import gather
        period=SimpleNamespace(business_id=self.business.pk,business=self.business,start_date=date(2026,10,1),end_date=date(2026,10,31))
        invoices,sources,*_=gather(period)
        self.assertEqual([inv.pk for inv in invoices],[a.invoice_id]);self.assertEqual(sources[0]['date'],date(2026,10,20))
        self.assertEqual(sources[0]['status'],'review')

    def test_export_has_blank_unknown_date_and_separate_evidence_and_upload_time(self):
        _,a=self.upload(fixtures.TEXT.replace('Invoice Date: 20/11/2026\n',''))
        workbook=load_workbook(BytesIO(invoice_batch_workbook_response(a.batch).content))
        row=list(workbook['Invoices'].values)[1]
        self.assertIsNone(row[2]);self.assertEqual(row[16],'Unknown — review required');self.assertTrue(row[19])

    def test_generated_record_pdf_never_prints_unknown_storage_date_as_invoice_date(self):
        _,a=self.upload(fixtures.TEXT.replace('Invoice Date: 20/11/2026\n',''))
        from .pdf_generator import invoice_pdf_data,generate_invoice_pdf
        data=invoice_pdf_data(a.invoice)
        self.assertEqual(data['date'],'Unknown - review required');self.assertIn('uploaded',data)
        with pdfplumber.open(BytesIO(generate_invoice_pdf(a.invoice))) as pdf:
            text='\n'.join(page.extract_text() or '' for page in pdf.pages)
        self.assertIn('Unknown - review required',text);self.assertIn('Uploaded',text)

    def test_dashboard_recent_invoices_show_unknown_instead_of_placeholder(self):
        _,a=self.upload(fixtures.TEXT.replace('Invoice Date: 20/11/2026\n',''))
        a.invoice.invoice_type='sales';a.invoice.save(update_fields=['invoice_type'])
        from analytics.views import dashboard_view
        from django.test import RequestFactory
        request=RequestFactory().get('/analytics/');request.user=self.user
        response=dashboard_view(request)
        self.assertContains(response,'Unknown — review required');self.assertContains(response,'Uploaded:')

    def test_legacy_verified_date_remains_saved_without_extraction_history(self):
        invoice=Invoice.objects.create(user=self.user,invoice_type='sales',invoice_number='LEGACY-DATE',invoice_date=date(2020,10,1),status='verified')
        self.assertEqual(invoice.source_dates['invoice_date'],date(2020,10,1));self.assertEqual(invoice.source_dates['invoice_state'],'saved')
