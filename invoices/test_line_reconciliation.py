"""Line evidence is reconciled conservatively without rewriting source candidates."""
from copy import deepcopy
from decimal import Decimal as D
import pdfplumber
from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from accounts.models import BusinessProfile
from .extraction import line_financial_reconciliation, parse_document
from .models import InvoiceExtractionAttempt as Attempt, InvoiceExtractionReview as Review, InvoicePayment

SALES='sample_data/invoices/mismatch_invoice_samples/SALE-AC-2026-009-LINE-MISMATCH.pdf'
PURCHASE='sample_data/invoices/mismatch_invoice_samples/PINE-P-2026-003-MISMATCH.pdf'


def parsed_pdf(path):
    with pdfplumber.open(settings.BASE_DIR/path) as pdf:
        pages=[{'page':i,'text':p.extract_text() or '', 'tables':p.extract_tables(),'method':'embedded'} for i,p in enumerate(pdf.pages,1)]
    return parse_document(pages,'digital_pdf')


def candidate(**updates):
    data=dict(pricing_mode='exclusive', supply_category='standard', supplier_state_code='27',place_of_supply='27',
        invoice_discount_type='fixed',invoice_discount_value='0',taxable_amount='200',cgst_amount='18',sgst_amount='18',igst_amount='0',other_charges='0',rounding_adjustment='0',total_amount='236',
        line_items=[dict(quantity='2',rate='100',discount='0',discount_type='fixed',tax_rate='18',tax_treatment='taxable',taxable_amount='200',tax_amount='36',total_amount='236')])
    data.update(updates)
    return data


class LineReconciliationRulesTests(SimpleTestCase):
    def test_exact_sales_pdf_preserves_unknowns_and_reports_definite_50(self):
        result=parsed_pdf(SALES)
        self.assertEqual(result['financial_reconciliation']['status'],'consistent')
        self.assertEqual(result['line_financial_reconciliation']['status'],'mismatch')
        warnings=' '.join(result['validation_errors'])
        for value in ('200.00','250.00','50.00','Header-versus-line','reconciliation is incomplete'):self.assertIn(value,warnings)
        for field in ('pricing_mode','invoice_discount_value','rounding_adjustment'):self.assertIsNone(result[field])
        self.assertIsNone(result['line_items'][0]['discount'])
        self.assertEqual(result['total_amount'],D('295'));self.assertEqual(result['taxable_amount'],D('250'))
        self.assertIsNone(result['line_financial_reconciliation']['calculated_total_before_rounding'])
        for field in ('line_items','taxable_amount','total_amount'):self.assertEqual(result['confidence'][field]['level'],'conflicting')

    def test_exact_purchase_pdf_keeps_header_200_warning(self):
        result=parsed_pdf(PURCHASE)
        for value in ('15950.00','15750.00','200.00'):self.assertIn(value,' '.join(result['validation_errors']))
        self.assertEqual(result['total_amount'],D('15950'));self.assertIsNone(result['rounding_adjustment'])
        self.assertEqual(result['confidence']['total_amount']['level'],'conflicting')

    def test_known_inputs_compare_lines_header_tax_and_grand_total(self):
        data=candidate(taxable_amount='250',cgst_amount='22.50',sgst_amount='22.50',total_amount='295')
        data['line_items'][0].update(taxable_amount='250',tax_amount='45',total_amount='295')
        before=deepcopy(data);check=line_financial_reconciliation(data)
        self.assertEqual(data,before);self.assertEqual(check['status'],'mismatch')
        for value in ('50.00','9.00','59.00'):self.assertIn(value,' '.join(check['warnings']))
        self.assertEqual(check['calculated_taxable'],D('200'));self.assertEqual(check['calculated_tax'],D('36'))

    def test_missing_line_and_header_inputs_are_incomplete(self):
        for field in ('quantity','rate','discount','discount_type','tax_rate','tax_treatment','pricing_mode','invoice_discount_value','other_charges'):
            with self.subTest(field=field):
                data=candidate()
                if field in data:data[field]=None
                else:data['line_items'][0][field]=None
                before=deepcopy(data);check=line_financial_reconciliation(data)
                self.assertEqual(check['status'],'incomplete');self.assertIn('incomplete',' '.join(check['warnings']));self.assertEqual(data,before)

    def test_unknown_discount_does_not_imply_exclusive_undiscounted_total(self):
        data=candidate(taxable_amount='150',total_amount='177',cgst_amount='13.50',sgst_amount='13.50',pricing_mode=None)
        data['line_items'][0].update(discount=None,taxable_amount='150',tax_amount=None,total_amount=None)
        check=line_financial_reconciliation(data)
        self.assertEqual(check['status'],'incomplete');self.assertIsNone(check['calculated_taxable'])
        self.assertNotIn('financial discrepancy',' '.join(check['warnings']))

    def test_missing_state_still_checks_base_and_aggregate_tax(self):
        check=line_financial_reconciliation(candidate(supplier_state_code=None,cgst_amount='20',sgst_amount='20'))
        self.assertEqual(check['calculated_taxable'],D('200'));self.assertEqual(check['status'],'mismatch')
        self.assertIn('explicit supported supplier state',' '.join(check['warnings']))
        self.assertIn('extracted GST total INR 40.00',' '.join(check['warnings']))

    def test_valid_exclusive_and_inclusive_pricing(self):
        self.assertEqual(line_financial_reconciliation(candidate())['warnings'],[])
        data=candidate(pricing_mode='inclusive',taxable_amount='1000.03',cgst_amount='90',sgst_amount='90',total_amount='1180.03')
        data['line_items'][0].update(quantity='1',rate='1180.03',taxable_amount='1000.03',tax_amount='180',total_amount='1180.03')
        self.assertEqual(line_financial_reconciliation(data)['warnings'],[])

    def test_valid_line_and_invoice_percentage_discounts(self):
        data=candidate(invoice_discount_type='percent',invoice_discount_value='5',taxable_amount='171',cgst_amount='15.39',sgst_amount='15.39',total_amount='201.78')
        data['line_items'][0].update(discount='10',discount_type='percent',taxable_amount='171',tax_amount='30.78',total_amount='201.78')
        self.assertEqual(line_financial_reconciliation(data)['warnings'],[])

    def test_valid_mixed_rates_and_interstate_split(self):
        data=candidate(taxable_amount='200',cgst_amount='0',sgst_amount='0',igst_amount='23',total_amount='223',place_of_supply='29')
        data['line_items']=[dict(quantity='1',rate='100',discount='0',discount_type='fixed',tax_rate=rate,tax_treatment='taxable',taxable_amount='100',tax_amount=rate,total_amount=total) for rate,total in [('18','118'),('5','105')]]
        self.assertEqual(line_financial_reconciliation(data)['warnings'],[])

    def test_explicit_rounding_and_charges_do_not_create_false_warning(self):
        for adjustment,total in [('0.50','236.50'),('-0.50','235.50'),('1','237'),('-1','235')]:
            with self.subTest(adjustment=adjustment):self.assertEqual(line_financial_reconciliation(candidate(rounding_adjustment=adjustment,total_amount=total))['warnings'],[])
        self.assertEqual(line_financial_reconciliation(candidate(other_charges='20',total_amount='256'))['warnings'],[])

    def test_missing_rounding_requires_evidence_for_small_residual(self):
        self.assertEqual(line_financial_reconciliation(candidate(rounding_adjustment=None))['warnings'],[])
        self.assertEqual(line_financial_reconciliation(candidate(rounding_adjustment=None,total_amount='236.50'))['status'],'incomplete')
        self.assertEqual(line_financial_reconciliation(candidate(rounding_adjustment=None,total_amount='238'))['status'],'mismatch')

    def test_missing_or_conflicting_lines_cannot_be_reported_consistent(self):
        for rows in ([],[dict(quantity=None,rate='100')],[dict(quantity='2',rate=None)]):
            self.assertEqual(line_financial_reconciliation(candidate(line_items=rows))['status'],'incomplete')

    def test_printed_line_taxable_evidence_is_checked_even_without_rates(self):
        data=candidate(taxable_amount='250')
        data['line_items'][0].update(rate=None)
        check=line_financial_reconciliation(data)
        self.assertEqual(check['status'],'mismatch')
        self.assertIn('sum of extracted line taxable values INR 200.00',' '.join(check['warnings']))
        self.assertIn('unexplained excess INR 50.00',' '.join(check['warnings']))

    def test_feature_library_valid_pdfs_do_not_report_false_line_mismatches(self):
        for name in ('EXCLUSIVE-18','INCLUSIVE-ROUNDING','DISCOUNTS','MIXED-RATES','ROUND-OFF','MULTIPAGE'):
            with self.subTest(name=name):
                check=parsed_pdf('sample_data/feature_acceptance/documents/'+name+'.pdf')['line_financial_reconciliation']
                # Missing source evidence requires review; it must not become an invented discrepancy.
                self.assertEqual(check['status'],'incomplete')
                self.assertNotIn('financial discrepancy',' '.join(check['warnings']))


class LineReconciliationWorkflowTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('line-review-owner',email='line-owner@example.test')
        self.business=BusinessProfile.objects.create(user=self.user,business_name='Synthetic Test Business',gstin='27AAACS9000A1ZY')
        self.client.force_login(self.user)

    def test_exact_sales_upload_current_warnings_and_immutable_history(self):
        response=self.client.post(reverse('invoice_upload'),{'invoice_type':'sales','source_files':SimpleUploadedFile('sales.pdf',(settings.BASE_DIR/SALES).read_bytes(),content_type='application/pdf')})
        self.assertEqual(response.status_code,302);attempt=Attempt.objects.get();original=deepcopy(attempt.result)
        self.assertEqual(attempt.result['line_financial_reconciliation']['status'],'mismatch')
        # The old result has only historical notices. Current checks must still run read-only.
        old=deepcopy(original);old.pop('line_financial_reconciliation')
        old['validation_errors']=[w for w in old['validation_errors'] if w not in original['line_financial_reconciliation']['warnings']]
        Attempt.objects.filter(pk=attempt.pk).update(result=old)
        for name,args in [('invoice_detail',[attempt.invoice_id]),('extraction_review',[attempt.pk])]:
            page=self.client.get(reverse(name,args=args));self.assertEqual(page.status_code,200)
            for value in ('Header-versus-line financial discrepancy','250.00','200.00','50.00','reconciliation is incomplete','Original extraction warnings (history)'):self.assertContains(page,value)
        attempt.refresh_from_db();self.assertEqual(attempt.result,old)
        self.client.post(reverse('invoice_verify',args=[attempt.invoice_id]),{'confirm':'on'})
        attempt.invoice.refresh_from_db();self.assertEqual(attempt.invoice.status,'review');self.assertEqual(attempt.invoice.total_amount,D('295'))
        self.assertFalse(Review.objects.exists());self.assertFalse(InvoicePayment.objects.exists());self.assertEqual(Attempt.objects.count(),1)
