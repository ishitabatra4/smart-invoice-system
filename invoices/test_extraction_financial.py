"""Financial extraction evidence must not hide mismatches or mutate history."""
from copy import deepcopy
from datetime import date
from decimal import Decimal as D
import pdfplumber
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase,TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from accounts.models import BusinessProfile,BusinessParty
from analytics.services import build_analytics_context
from . import test_extraction as fixtures
from .extraction import parse_document,financial_reconciliation
from .models import Invoice,InvoiceExtractionAttempt as Attempt,InvoiceExtractionReview as Review,InvoicePayment

SAMPLE='sample_data/invoices/mismatch_invoice_samples/PINE-P-2026-003-MISMATCH.pdf'


def parse_text(text):
    return parse_document([{'page':1,'text':text,'method':'embedded','tables':[]}],'digital_pdf')


def source_text():
    with pdfplumber.open(settings.BASE_DIR/SAMPLE) as pdf:
        return '\n'.join(page.extract_text() or '' for page in pdf.pages)


class FinancialExtractionRulesTests(SimpleTestCase):
    def test_exact_sample_missing_rounding_reports_200_without_replacing_values(self):
        result=parse_text(source_text())
        self.assertEqual(result['total_amount'],D('15950'));self.assertEqual(result['other_charges'],D('0'))
        self.assertIsNone(result['rounding_adjustment'])
        self.assertEqual(result['financial_reconciliation']['component_total'],D('15750'))
        self.assertEqual(result['financial_reconciliation']['residual'],D('200'))
        self.assertEqual(result['financial_reconciliation']['status'],'mismatch')
        self.assertEqual(result['confidence']['total_amount']['level'],'conflicting')
        warning=' '.join(result['validation_errors'])
        for value in ('15950.00','15750.00','200.00','Round-off is unknown'):self.assertIn(value,warning)

    def test_correct_sample_total_without_rounding_is_consistent(self):
        result=parse_text(source_text().replace('Grand Total 15950.00','Grand Total 15750.00'))
        self.assertIsNone(result['rounding_adjustment']);self.assertEqual(result['total_amount'],D('15750'))
        self.assertEqual(result['financial_reconciliation']['status'],'consistent')
        self.assertEqual(result['financial_reconciliation']['warnings'],[])
        self.assertEqual(result['confidence']['total_amount']['level'],'low')

    def test_explicit_zero_rounding_keeps_mismatch_detection(self):
        result=parse_text(source_text()+'\nRounding Adjustment: 0.00\n')
        self.assertEqual(result['rounding_adjustment'],0)
        self.assertEqual(result['financial_reconciliation']['status'],'mismatch')
        self.assertIn('200.00',' '.join(result['validation_errors']))

    def test_positive_negative_and_boundary_rounding(self):
        for adjustment,total in [('0.50','1180.50'),('-0.50','1179.50'),('1.00','1181.00'),('-1.00','1179.00')]:
            with self.subTest(adjustment=adjustment):
                text=fixtures.TEXT.replace('Rounding Adjustment: INR 0.00','Rounding Adjustment: INR '+adjustment).replace('Grand Total: INR 1180.00','Grand Total: INR '+total)
                result=parse_text(text)
                self.assertEqual(result['financial_reconciliation']['warnings'],[])
                self.assertEqual(result['rounding_adjustment'],D(adjustment));self.assertEqual(result['total_amount'],D(total))

    def test_unprinted_small_rounding_requires_review_without_inference(self):
        for total in ('1180.50','1179.50','1181.00','1179.00'):
            with self.subTest(total=total):
                result=parse_text(fixtures.TEXT.replace('Rounding Adjustment: INR 0.00\n','').replace('Grand Total: INR 1180.00','Grand Total: INR '+total))
                self.assertIsNone(result['rounding_adjustment']);self.assertEqual(result['total_amount'],D(total))
                self.assertEqual(result['financial_reconciliation']['status'],'incomplete')
                self.assertEqual(result['confidence']['total_amount']['level'],'low')

    def test_residual_above_bound_and_unsupported_explicit_rounding(self):
        for adjustment,total in [(None,'1181.01'),(None,'1178.99'),('1.01','1181.01'),('-1.01','1178.99'),('200.00','1380.00')]:
            with self.subTest(adjustment=adjustment,total=total):
                text=fixtures.TEXT.replace('Rounding Adjustment: INR 0.00\n','' if adjustment is None else 'Rounding Adjustment: INR '+adjustment+'\n').replace('Grand Total: INR 1180.00','Grand Total: INR '+total)
                result=parse_text(text);self.assertEqual(result['financial_reconciliation']['status'],'mismatch')
                self.assertEqual(result['confidence']['total_amount']['level'],'conflicting')
                self.assertEqual(result['total_amount'],D(total))

    def test_missing_other_charges_is_incomplete_not_zero(self):
        result=parse_text(fixtures.TEXT.replace('Other Charges: INR 0.00\n',''))
        self.assertIsNone(result['other_charges']);self.assertEqual(result['financial_reconciliation']['status'],'incomplete')
        self.assertIn('other charges',' '.join(result['validation_errors']))
        self.assertEqual(result['confidence']['total_amount']['level'],'low')

    def test_conflicting_other_charges_is_incomplete_and_evidence_retained(self):
        result=parse_text(fixtures.TEXT+'Other Charges: INR 20.00\n')
        self.assertIsNone(result['other_charges']);self.assertEqual(result['confidence']['other_charges']['level'],'conflicting')
        self.assertEqual(result['confidence']['other_charges']['candidates'],['0.00','20.00'])
        self.assertEqual(result['financial_reconciliation']['status'],'incomplete');self.assertEqual(result['confidence']['total_amount']['level'],'low')

    def test_missing_required_components_and_partial_residual(self):
        for field,label in [('taxable_amount','Taxable Value: INR 1000.00\n'),('cgst_amount','CGST 9%: INR 90.00\n'),('sgst_amount','SGST 9%: INR 90.00\n'),('igst_amount','IGST: INR 0.00\n'),('total_amount','Grand Total: INR 1180.00\n')]:
            with self.subTest(field=field):
                result=parse_text(fixtures.TEXT.replace(label,''));self.assertIsNone(result[field])
                self.assertEqual(result['financial_reconciliation']['status'],'incomplete')
                self.assertNotEqual(result['confidence']['total_amount']['level'],'high')
                if field=='cgst_amount':
                    self.assertEqual(result['financial_reconciliation']['component_total'],D('1090'))
                    self.assertEqual(result['financial_reconciliation']['residual'],D('90'))
                    self.assertIn('known component subtotal',' '.join(result['validation_errors']))

    def test_conflicting_total_confidence_is_not_upgraded(self):
        result=parse_text(fixtures.TEXT+'Grand Total: INR 1200.00\n')
        self.assertIsNone(result['total_amount']);self.assertEqual(result['confidence']['total_amount']['level'],'conflicting')
        self.assertEqual(result['financial_reconciliation']['status'],'incomplete')

    def test_helper_is_read_only_and_handles_invalid_retained_evidence(self):
        data={'taxable_amount':'NaN','total_amount':'15950.00'};before=deepcopy(data)
        self.assertEqual(financial_reconciliation(data)['status'],'incomplete');self.assertEqual(data,before)

    def test_valid_existing_extraction_is_unchanged(self):
        result=parse_text(fixtures.TEXT)
        self.assertEqual(result['financial_reconciliation']['warnings'],[])
        self.assertEqual(result['total_amount'],D('1180'));self.assertEqual(result['confidence']['total_amount']['level'],'low')


class FinancialExtractionWorkflowTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('financial-extraction-owner',email='financial-extraction@example.test')
        self.business=BusinessProfile.objects.create(user=self.user,business_name='Synthetic Buyer',gstin='27AAACS9000A1ZY')
        self.vendor=BusinessParty.objects.create(business=self.business,name='Synthetic Supplier',party_type='vendor',gstin='27AAACS9002A1ZW')
        self.client.force_login(self.user)
    def upload(self,body=None):
        return self.client.post(reverse('invoice_upload'),{'invoice_type':'purchase','source_files':SimpleUploadedFile('synthetic.pdf',body or fixtures.text_pdf(),content_type='application/pdf')})
    def payload(self,attempt,**updates):return fixtures.ExtractionWorkflowTests.payload(self,attempt,**updates)
    def totals(self):return build_analytics_context(self.user,date(2026,1,1),date(2026,12,31),'custom')['totals']

    def test_exact_sample_warning_is_displayed_on_review_and_detail(self):
        self.upload((settings.BASE_DIR/SAMPLE).read_bytes());attempt=Attempt.objects.first();before=deepcopy(attempt.result)
        for name,args in [('extraction_review',[attempt.pk]),('invoice_detail',[attempt.invoice_id])]:
            response=self.client.get(reverse(name,args=args));self.assertEqual(response.status_code,200)
            for value in ('15950.00','15750.00','200.00','Current validation and review requirements','Original extraction warnings (history)'):self.assertContains(response,value)
        attempt.refresh_from_db();self.assertEqual(attempt.result,before);self.assertIsNone(attempt.result['rounding_adjustment'])
        self.assertEqual(attempt.invoice.total_amount,D('15950'));self.assertEqual(attempt.invoice.status,'review')

    def test_old_five_notices_and_four_history_are_explained_without_rewriting(self):
        self.upload((settings.BASE_DIR/SAMPLE).read_bytes());attempt=Attempt.objects.first()
        # Recreate an immutable pre-fix result directly in the disposable test database.
        old=deepcopy(attempt.result);old.pop('financial_reconciliation',None)
        line_warnings=old.pop('line_financial_reconciliation',{}).get('warnings',[])
        old['validation_errors']=[w for w in old['validation_errors'] if w not in line_warnings]
        old['validation_errors']=[w for w in old['validation_errors'] if 'reconcile with saved components' not in w]
        old['confidence']['total_amount'].update(level='high',reason='Historical label evidence only.')
        Attempt.objects.filter(pk=attempt.pk).update(result=old)
        Invoice.objects.filter(pk=attempt.invoice_id).update(validation_errors=old['validation_errors']+['Extraction is provisional. Confirm the reviewed data, then explicitly verify it.'])
        self.assertEqual(len(old['validation_errors']),4)
        response=self.client.get(reverse('invoice_list'));self.assertContains(response,'5 stored notice(s)')
        for name,args in [('invoice_detail',[attempt.invoice_id]),('extraction_review',[attempt.pk])]:
            response=self.client.get(reverse(name,args=args));self.assertContains(response,'5 stored notice(s)');self.assertContains(response,'4 historical extraction warning(s)');self.assertContains(response,'200.00');self.assertContains(response,'not interchangeable')
        attempt.refresh_from_db();self.assertEqual(attempt.result,old);self.assertEqual(len(attempt.invoice.validation_errors),5)

    def test_confirmation_and_unconfirmed_verification_reject_mismatch(self):
        self.upload(fixtures.text_pdf(fixtures.TEXT.replace('Grand Total: INR 1180.00','Grand Total: INR 1200.00')));a=Attempt.objects.first()
        response=self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a,total_amount='1200'))
        self.assertEqual(response.status_code,200);self.assertContains(response,'server-calculated value 1180.00');self.assertFalse(Review.objects.exists())
        self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'})
        a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'review');self.assertFalse(InvoicePayment.objects.exists())

    def test_confirmation_rejects_balanced_header_that_disagrees_with_lines(self):
        text=fixtures.TEXT.replace('Taxable Value: INR 1000.00','Taxable Value: INR 1250.00').replace('90.00','112.50').replace('Grand Total: INR 1180.00','Grand Total: INR 1475.00')
        self.upload(fixtures.text_pdf(text));attempt=Attempt.objects.get();original=deepcopy(attempt.result)
        self.assertEqual(original['financial_reconciliation']['status'],'consistent')
        self.assertEqual(original['line_financial_reconciliation']['status'],'mismatch')
        payload=self.payload(attempt,taxable_amount='1250',cgst_amount='112.50',sgst_amount='112.50',total_amount='1475')
        response=self.client.post(reverse('extraction_review',args=[attempt.pk]),payload)
        self.assertEqual(response.status_code,200);self.assertContains(response,'server-calculated value 1000.00')
        self.assertFalse(Review.objects.exists());self.assertFalse(InvoicePayment.objects.exists())
        attempt.refresh_from_db();self.assertEqual(attempt.result,original);self.assertEqual(attempt.invoice.status,'review')

    def test_duplicate_and_mismatch_do_not_change_confirmed_totals_or_payments(self):
        Invoice.objects.create(user=self.user,invoice_type='sales',invoice_number='SYNTHETIC-BASELINE',invoice_date=date(2026,11,1),taxable_amount=1000,cgst_amount=90,sgst_amount=90,total_amount=1180,status='verified')
        before=self.totals();before_payments=list(InvoicePayment.objects.values())
        body=fixtures.text_pdf(fixtures.TEXT.replace('Grand Total: INR 1180.00','Grand Total: INR 1200.00'))
        self.upload(body);self.upload(body)
        self.assertEqual(Attempt.objects.count(),2);self.assertEqual(Attempt.objects.first().status,'blocked')
        self.assertEqual(self.totals(),before);self.assertEqual(list(InvoicePayment.objects.values()),before_payments)
        self.assertEqual(Invoice.objects.filter(status__in=['verified','paid']).count(),1)

    def test_corrected_confirmed_input_is_not_blocked_by_historical_machine_warning(self):
        self.upload(fixtures.text_pdf(fixtures.TEXT.replace('Grand Total: INR 1180.00','Grand Total: INR 1200.00')));a=Attempt.objects.first();original=deepcopy(a.result)
        response=self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a));self.assertEqual(response.status_code,302)
        page=self.client.get(reverse('invoice_detail',args=[a.invoice_id]))
        self.assertEqual(page.context['current_notices'],['Confirmed draft still requires separate explicit financial verification.'])
        self.assertContains(page,'1200.00') # Historical evidence remains visible.
        a.refresh_from_db();self.assertEqual(a.result,original)
        self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'verified')

    def test_stale_confirmed_snapshot_is_identified_and_verification_blocked(self):
        self.upload();a=Attempt.objects.first();self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a))
        Invoice.objects.filter(pk=a.invoice_id).update(total_amount=D('1200'))
        self.assertContains(self.client.get(reverse('invoice_detail',args=[a.invoice_id])),'confirmed snapshot is stale')
        self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'review')
