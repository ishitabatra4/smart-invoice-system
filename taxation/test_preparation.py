"""Synthetic, isolated tax working-paper tests. No live government/SMTP calls."""
import calendar
import json
import tempfile
import uuid
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal as D
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction, IntegrityError
from django.test import TestCase, SimpleTestCase, RequestFactory, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
import pdfplumber
from accounts.models import BusinessProfile
from audit.models import AuditEvent
from invoices import test_manual as fixtures
from invoices import test_adjustments as adjustments
from invoices.gstin import ALPHABET
from invoices.models import Invoice, InvoicePayment
from expenses.models import ExpenseITCReview
from expenses.services import source_evidence, fingerprint, review_itc
from .models import TaxPeriod, TaxWorkingPaper, TaxWorkingPaperEvent, ReconciliationRun, ReconciliationRecord
from . import preparation as p
from .tax_rules import estimate_income, b2c_bucket, round_ten, rule_bundle, INCOME_RULES
from .reconciliation import parse_uploaded_file, ReconciliationFileError


def valid_gstin(prefix='27ABCDE1234F1Z'):
    total = 0
    for index, char in enumerate(reversed(prefix)):
        product = ALPHABET.index(char) * (2 if index % 2 == 0 else 1)
        total += product // 36 + product % 36
    return prefix + ALPHABET[(36-total % 36) % 36]


def income_inputs(**updates):
    values = dict(taxpayer='resident_individual', regime='new', resident_confirmed=True, ordinary_only=True, taxable_income='1600000', deductions='0', assessment_year='2026-27')
    values.update(updates)
    return values


class RuleTests(SimpleTestCase):
    def period(self, year=2025, **updates):
        data = dict(kind='financial_year', start_date=date(year, 4, 1), end_date=date(year+1, 3, 31)); data.update(updates)
        return SimpleNamespace(**data)

    def estimate(self, **kwargs):
        return estimate_income(self.period(), income_inputs(**kwargs), 'proprietorship')

    def test_fy_boundaries_and_leap_month(self):
        for kind, start, end in [('financial_year', date(2023,4,1), date(2024,3,31)), ('monthly', date(2024,2,1), date(2024,2,29)), ('quarterly', date(2026,10,1), date(2026,12,31)), ('custom', date(2026,1,2), date(2026,1,2))]:
            p.validate_period(kind, start, end)

    def test_invalid_periods(self):
        for kind, start, end in [('monthly', date(2024,2,1), date(2024,2,28)), ('financial_year', date(2026,1,1), date(2026,12,31)), ('quarterly', date(2026,2,1), date(2026,4,30)), ('custom', date(2026,1,2), date(2026,1,1)), ('custom', date(2024,1,1), date(2025,12,31)), ('filed', date(2026,1,1), date(2026,1,1))]:
            with self.subTest(kind=kind, start=start), self.assertRaises(ValidationError):
                p.validate_period(kind, start, end)

    def test_b2cl_effective_thresholds(self):
        self.assertEqual(b2c_bucket(date(2024,7,31), D('250000'), True), 'B2CS')
        self.assertEqual(b2c_bucket(date(2024,7,31), D('250000.01'), True), 'B2CL')
        self.assertEqual(b2c_bucket(date(2024,8,1), D('100000'), True), 'B2CS')
        self.assertEqual(b2c_bucket(date(2024,8,1), D('100000.01'), True), 'B2CL')
        self.assertEqual(b2c_bucket(date(2026,8,1), D('900000'), False), 'B2CS')
        self.assertEqual(b2c_bucket(date(2017,6,30), D('900000'), True), 'unresolved')

    def test_income_slab_values(self):
        for income, base, final in [('400000','0','0'), ('800000','20000','0'), ('1200000','60000','0'), ('1600000','120000','124800'), ('2000000','200000','208000'), ('2400000','300000','312000'), ('3000000','480000','499200'), ('5000000','1080000','1123200')]:
            with self.subTest(income=income):
                result = self.estimate(taxable_income=income)
                self.assertEqual(result['base_tax'], D(base)); self.assertEqual(result['estimated_tax'], D(final))

    def test_rebate_marginal_relief(self):
        result = self.estimate(taxable_income='1200010')
        self.assertEqual(result['rebate_marginal_relief'], D('59991.50'))
        self.assertEqual(result['estimated_tax'], D('10'))
        self.assertEqual(self.estimate(taxable_income='1300000')['rebate_marginal_relief'], 0)

    def test_ignores_paise_before_rounding(self):
        self.assertEqual(round_ten('12344.99'), D('12340'))
        self.assertEqual(round_ten('12345.01'), D('12350'))
        self.assertEqual(self.estimate(taxable_income='1600004.99')['rounded_taxable_income'], D('1600000'))

    def test_supported_tax_year_2026_act(self):
        result = estimate_income(self.period(2026), income_inputs(assessment_year='not_applicable'), 'individual')
        self.assertEqual(result['estimated_tax'], D('124800'))
        self.assertIsNone(result['rule']['assessment_year']); self.assertEqual(result['rule']['tax_year'], '2026-27')

    def test_unsupported_businesses(self):
        for business in ('private_limited','public_limited','llp','partnership','other'):
            result = estimate_income(self.period(), income_inputs(), business)
            self.assertEqual(result['status'], 'manual_review_required'); self.assertIsNone(result['estimated_tax'])

    def test_unsupported_inputs_require_review(self):
        for change in [dict(taxpayer='other'),dict(regime='old'),dict(resident_confirmed=False),dict(ordinary_only=False),dict(deductions='100'),dict(taxable_income=None),dict(taxable_income='-1'),dict(taxable_income='5000010'),dict(assessment_year='2027-28')]:
            with self.subTest(change=change):
                self.assertIsNone(self.estimate(**change)['estimated_tax'])

    def test_unknown_year_and_month_do_not_estimate(self):
        self.assertIsNone(estimate_income(self.period(2027),income_inputs(),'individual')['estimated_tax'])
        self.assertIsNone(estimate_income(self.period(kind='monthly'),income_inputs(),'individual')['estimated_tax'])

    def test_rule_bundle_is_independent_versioned_copy(self):
        bundle=rule_bundle();bundle['income_rules'][2025]['slabs'][0][0]='bad'
        self.assertEqual(INCOME_RULES[2025]['slabs'][0][0],'400000')
        self.assertTrue(all(r['url'].startswith('https://') and r['verified_on']=='2026-11-22' for r in rule_bundle()['references'].values()))


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class PreparationTests(TestCase):
    def setUp(self):
        self.storage=tempfile.TemporaryDirectory();self.settings_context=override_settings(MEDIA_ROOT=self.storage.name,PRIVATE_EXPENSE_ROOT=Path(self.storage.name)/'private');self.settings_context.enable();self.addCleanup(self.storage.cleanup);self.addCleanup(self.settings_context.disable)
        fixtures.ManualInvoiceTests.setUp(self)
        self.business.gstin=valid_gstin();self.business.save()
        self.vendor.gstin=valid_gstin();self.vendor.save()
        self.request=RequestFactory().post('/tax/prepare/');self.request.user=self.user
        self.period=p.create_period(self.request,'monthly',date(2026,10,1),date(2026,10,31))
        self.invoice=self.create()
        fixtures.ManualInvoiceTests.finalize(self,self.invoice);self.invoice.refresh_from_db()

    def payload(self,*args,**kwargs):return fixtures.ManualInvoiceTests.payload(self,*args,**kwargs)
    def create(self,kind='sales',**kwargs):return fixtures.ManualInvoiceTests.create(self,kind,**kwargs)
    def inputs(self,**updates):
        values=dict(ordinary_domestic=True,aato_band='up_to_5cr',import_runs=[],registration={str(self.invoice.pk):{'status':'unregistered','reason':'Customer independently confirms not registered'}})
        values.update(updates);return values
    def preview(self,kind='gstr1',**updates):return p.preview(self.period,kind,self.inputs(**updates))[0]
    def paper(self,kind='gstr1',inputs=None,previous=None,key=None):
        inputs=inputs or self.inputs();data,_,_=p.preview(self.period,kind,inputs)
        return p.prepare(self.request,self.period,kind,inputs,key or uuid.uuid4(),p.digest(data),previous.pk if previous else None,True)
    def codes(self,data):return {w['code'] for w in data['warnings']}
    def purchase(self,**kwargs):
        inv=self.create('purchase',**kwargs);fixtures.ManualInvoiceTests.finalize(self,inv);inv.refresh_from_db();return inv
    def imported_run(self,invoice=None,**updates):
        inv=invoice or self.purchase()
        record=dict(supplier_gstin=inv.supplier_gstin,invoice_number=inv.invoice_number,invoice_date=inv.invoice_date,taxable_value=inv.taxable_amount,cgst_amount=inv.cgst_amount,sgst_amount=inv.sgst_amount,igst_amount=inv.igst_amount,total_tax=inv.total_tax);record.update(updates)
        upload=SimpleUploadedFile('extract.json',json.dumps([p.serial(record)]).encode(),content_type='application/json')
        run=ReconciliationRun.objects.create(user=self.user,return_period='2026-10',source_file=upload)
        ReconciliationRecord.objects.create(run=run,**record)
        return run, inv
    def fake_review(self,invoice,status):
        return ExpenseITCReview.objects.create(invoice=invoice,business=self.business,reviewer=self.user,status=status,reason='Synthetic evidence review',evidence=source_evidence(invoice),fingerprint=fingerprint(source_evidence(invoice)))

    def test_period_reuse_and_ownership(self):
        self.assertEqual(p.create_period(self.request,'monthly',self.period.start_date,self.period.end_date).pk,self.period.pk)
        self.assertEqual(self.period.status,'draft')
        with self.assertRaises(ValidationError):TaxPeriod.objects.create(business=self.other_business,created_by=self.user,kind='monthly',start_date=self.period.start_date,end_date=self.period.end_date)

    def test_period_overlap_warning(self):
        p.create_period(self.request,'financial_year',date(2026,4,1),date(2027,3,31))
        self.assertIn('overlap',self.codes(self.preview()))

    def test_b2cs_saved_values(self):
        d=self.preview();self.assertEqual(d['gstr1']['buckets']['B2CS']['total'],'224.20')
        self.assertEqual(d['liability']['net_output'],{'cgst':'17.10','sgst':'17.10','igst':'0.00'})
        self.assertEqual(d['gstr1']['hsn_invoice_evidence'][0]['code'],'9983')

    def test_registered_b2b_requires_valid_evidence(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(buyer_gstin=valid_gstin('29ABCDE1234F1Z'))
        d=self.preview(registration={str(self.invoice.pk):{'status':'registered','reason':'Registration independently reviewed'}})
        self.assertEqual(d['gstr1']['buckets']['B2B']['total'],'224.20')

    def test_missing_registration_not_inferred(self):
        d=self.preview(registration={});self.assertEqual(d['gstr1']['buckets']['unresolved']['total'],'224.20');self.assertIn('registration_unknown',self.codes(d))

    def test_invalid_gstin_never_becomes_unregistered(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(buyer_gstin='invalid')
        d=self.preview(registration={str(self.invoice.pk):{'status':'registered','reason':'Review'}})
        self.assertEqual(d['gstr1']['buckets']['B2B']['total'],'0.00');self.assertIn('recipient_gstin',self.codes(d))

    def test_unregistered_conflict_and_missing_reason(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(buyer_gstin=valid_gstin())
        self.assertIn('conflicting_registration',self.codes(self.preview()))
        self.assertIn('registration_evidence',self.codes(self.preview(registration={str(self.invoice.pk):{'status':'registered','reason':''}})))

    def test_interstate_b2cl_threshold_uses_grand_total(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(place_of_supply='29',taxable_amount=D('100000'),cgst_amount=0,sgst_amount=0,igst_amount=D('18000'),total_amount=D('118000'),calculation_version=0)
        d=self.preview();self.assertEqual(d['gstr1']['buckets']['unresolved']['total'],'118000.00')
        # Preserve legacy unclassified evidence rather than inventing a classification.
        self.assertIn('tax_treatment',self.codes(d))

    def test_new_interstate_b2cl(self):
        inv=self.create(place_of_supply='29',**{'items-0-quantity':'1','items-0-rate':'100000','items-0-discount':'0'})
        fixtures.ManualInvoiceTests.finalize(self,inv)
        d=self.preview(registration={str(inv.pk):{'status':'unregistered','reason':'Independently confirmed'}})
        self.assertEqual(d['gstr1']['buckets']['B2CL']['total'],'118000.00')
        self.assertEqual(d['gstr1']['buckets']['B2CL']['igst'],'18000.00')

    def test_drafts_excluded(self):
        draft=self.create();d=self.preview();self.assertIn(draft.pk,[x['id'] for x in d['excluded']]);self.assertEqual(len(d['included']),1)

    def test_cancelled_source_excluded_and_flagged(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(status='cancelled',document_state='cancelled')
        d=self.preview();self.assertEqual(d['gstr1']['original_outward']['total'],'0.00');self.assertIn('cancelled_source',self.codes(d))

    def test_uploaded_unreviewed_not_recognized(self):
        Invoice.objects.create(user=self.user,invoice_type='sales',invoice_number='SCAN',invoice_date=self.period.start_date,status='uploaded',total_amount=999)
        self.assertEqual(len(self.preview()['included']),1)

    def test_saved_header_mismatch_blocks_prepare(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(total_amount=999)
        self.assertIn('amount_mismatch',self.codes(self.preview()))
        with self.assertRaises(ValidationError):self.paper()

    def test_saved_line_header_mismatch_blocks(self):
        self.invoice.line_items.update(taxable_amount=123)
        self.assertIn('line_header_mismatch',self.codes(self.preview()))
        with self.assertRaises(ValidationError):self.paper()

    def test_missing_document_number_blocks(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(invoice_number='')
        with self.assertRaises(ValidationError):self.paper()

    def test_unknown_treatment_retained(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(treatment_breakdown={})
        d=self.preview();self.assertEqual(d['gstr1']['buckets']['unresolved']['taxable'],'190.00')

    def test_missing_supply_retained_unresolved(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(supply_category='export')
        self.assertIn('supply_classification',self.codes(self.preview()))

    def test_nonstandard_scope_blocks(self):
        with self.assertRaises(ValidationError):self.paper(inputs=self.inputs(ordinary_domestic=False))

    def test_invalid_business_gstin_blocks(self):
        self.business.gstin='invalid';self.business.save()
        with self.assertRaises(ValidationError):self.paper()

    def test_hsn_band_warning(self):
        self.assertIn('hsn_missing',self.codes(self.preview(aato_band='above_5cr')))
        self.assertIn('hsn_aato',self.codes(self.preview(aato_band='unknown')))

    def test_import_match_never_confirms_itc(self):
        run,inv=self.imported_run();d=self.preview(import_runs=[run.pk]);row=d['reconciliation']['book_records'][0]
        self.assertEqual(row['status'],'matched');self.assertEqual(row['itc_status'],'pending')
        self.assertEqual(d['liability']['reviewed_input']['cgst'],'0.00');self.assertEqual(ExpenseITCReview.objects.count(),0)
        self.assertEqual(d['source_evidence']['imports'][0]['validation'],'supported_user_extract')

    def test_import_no_evidence(self):
        self.purchase();self.assertEqual(self.preview()['reconciliation']['book_records'][0]['status'],'missing_evidence')

    def test_import_amount_mismatch(self):
        run,_=self.imported_run(taxable_value=D('1'))
        self.assertIn('amount_mismatch',self.preview(import_runs=[run.pk])['reconciliation']['book_records'][0]['flags'])

    def test_import_gstin_mismatch(self):
        run,_=self.imported_run(supplier_gstin=valid_gstin('07ABCDE1234F1Z'))
        self.assertIn('gstin_mismatch',self.preview(import_runs=[run.pk])['reconciliation']['book_records'][0]['flags'])

    def test_import_date_mismatch(self):
        run,_=self.imported_run(invoice_date=date(2026,10,2))
        self.assertIn('date_mismatch',self.preview(import_runs=[run.pk])['reconciliation']['book_records'][0]['flags'])

    def test_normalized_reference_requires_manual_review(self):
        run,_=self.imported_run(invoice_number='SUP001')
        self.assertIn('pending_review',self.preview(import_runs=[run.pk])['reconciliation']['book_records'][0]['flags'])

    def test_duplicate_across_runs(self):
        run,inv=self.imported_run();other,_=self.imported_run(invoice=inv)
        self.assertEqual(self.preview(import_runs=[run.pk,other.pk])['reconciliation']['book_records'][0]['status'],'duplicate')

    def test_duplicate_in_source_extract_detected(self):
        run,_=self.imported_run()
        with run.source_file.open('rb') as f:rows=json.load(f)
        run.source_file.save('duplicates.json',SimpleUploadedFile('duplicates.json',json.dumps(rows*2).encode()))
        self.assertEqual(self.preview(import_runs=[run.pk])['source_evidence']['imports'][0]['validation'],'duplicate')

    def test_original_parser_default_unchanged(self):
        row=dict(supplier_gstin=valid_gstin(),invoice_number='A',invoice_date='2026-10-01',taxable_value='100',cgst_amount='9',sgst_amount='9',igst_amount='0',total_tax='18')
        raw=json.dumps([row,row]).encode()
        self.assertEqual(len(parse_uploaded_file(SimpleUploadedFile('a.json',raw))),1)
        with self.assertRaises(ReconciliationFileError):parse_uploaded_file(SimpleUploadedFile('a.json',raw),reject_duplicates=True)

    def test_import_unmatched(self):
        run,_=self.imported_run(invoice_number='different')
        d=self.preview(import_runs=[run.pk]);self.assertEqual(d['reconciliation']['book_records'][0]['status'],'unmatched');self.assertEqual(len(d['reconciliation']['unmatched_imports']),1)

    def test_foreign_import_rejected(self):
        run=ReconciliationRun.objects.create(user=self.other,return_period='2026-10',source_file='absent.json')
        with self.assertRaises(ValidationError):self.preview(import_runs=[run.pk])

    def test_foreign_registration_rejected(self):
        with self.assertRaises(ValidationError):self.preview(registration={'99999':{'status':'unregistered','reason':'not ours'}})

    def test_import_month_warning(self):
        run,_=self.imported_run();run.return_period='2026-09';run.save()
        self.assertIn('import_period',self.codes(self.preview(import_runs=[run.pk])))

    def test_missing_import_file_is_disclosed(self):
        run,_=self.imported_run();Path(run.source_file.path).unlink()
        self.assertEqual(self.preview(import_runs=[run.pk])['reconciliation']['book_records'][0]['status'],'missing_evidence')

    def test_itc_pending_ineligible_potential_separate(self):
        inv=self.purchase()
        for state in ('pending','ineligible','potential'):
            with self.subTest(status=state):
                self.fake_review(inv,state);d=self.preview('gstr3b')
                self.assertEqual(d['gstr3b']['itc_buckets'][state]['cgst'],'17.10');self.assertEqual(d['liability']['reviewed_input']['cgst'],'0.00')

    def test_current_reviewed_components_only(self):
        inv=self.purchase();self.fake_review(inv,'confirmed')
        d=self.preview('gstr3b');self.assertEqual(d['liability']['reviewed_input']['cgst'],'17.10');self.assertEqual(d['liability']['component_difference']['cgst'],'0.00')

    def test_changed_itc_source_reverts_pending(self):
        inv=self.purchase();self.fake_review(inv,'confirmed');Invoice.objects.filter(pk=inv.pk).update(supplier_name='Changed snapshot')
        d=self.preview();self.assertEqual(d['liability']['reviewed_input']['cgst'],'0.00');self.assertIn('itc_pending',self.codes(d))

    def test_no_automatic_profit_or_deduction(self):
        self.purchase();d=self.preview('income_summary')
        self.assertIsNone(d['income_summary']['taxable_income']);self.assertIsNone(d['income_summary']['accounting_profit'])
        self.assertEqual(d['income_summary']['saved_analytics']['totals']['purchases'],'224.20')

    def test_preview_does_not_persist(self):
        before=Invoice.objects.values_list('pk','taxable_amount','total_amount').get(pk=self.invoice.pk)
        self.preview();self.assertEqual(TaxWorkingPaper.objects.count(),0);self.assertEqual(before,Invoice.objects.values_list('pk','taxable_amount','total_amount').get(pk=self.invoice.pk));self.assertEqual(InvoicePayment.objects.count(),0)

    def test_snapshot_complete_evidence(self):
        paper=self.paper();self.assertEqual(paper.status,'prepared');self.assertEqual(self.period.status,'prepared');self.assertEqual(paper.data['included'][0]['values']['total'],'224.20');self.assertEqual(len(paper.source_fingerprint),64);self.assertIn('source_evidence',paper.data)

    def test_duplicate_prepare_requires_revision(self):
        self.paper()
        with self.assertRaises(ValidationError):self.paper()
        self.assertEqual(TaxWorkingPaper.objects.count(),1)

    def test_idempotency_key(self):
        key=uuid.uuid4();first=self.paper(key=key);self.assertEqual(first.pk,self.paper(key=key).pk);self.assertEqual(TaxWorkingPaperEvent.objects.count(),1)

    def test_idempotency_changed_inputs_rejected(self):
        key=uuid.uuid4();self.paper(key=key)
        with self.assertRaises(ValidationError):self.paper(key=key,inputs=self.inputs(aato_band='unknown'))

    def test_review_confirmation_required(self):
        inputs=self.inputs();d,_,_=p.preview(self.period,'gstr1',inputs)
        with self.assertRaises(ValidationError):p.prepare(self.request,self.period,'gstr1',inputs,uuid.uuid4(),p.digest(d),None,False)

    def test_changed_preview_is_rejected(self):
        inputs=self.inputs();d,_,_=p.preview(self.period,'gstr1',inputs)
        Invoice.objects.filter(pk=self.invoice.pk).update(buyer_name='Different saved party')
        with self.assertRaises(ValidationError):p.prepare(self.request,self.period,'gstr1',inputs,uuid.uuid4(),p.digest(d),None,True)

    def test_model_and_queryset_immutability(self):
        paper=self.paper()
        for operation in (lambda:paper.save(),lambda:paper.delete(),lambda:TaxWorkingPaper.objects.filter(pk=paper.pk).update(data={}),lambda:TaxWorkingPaper.objects.filter(pk=paper.pk).delete(),lambda:paper.events.first().save(),lambda:paper.events.all().delete()):
            with self.assertRaises(ValidationError):operation()

    def test_snapshot_stale_body_retained(self):
        paper=self.paper();saved=deepcopy(paper.data);self.assertFalse(p.stale(paper))
        Invoice.objects.filter(pk=self.invoice.pk).update(buyer_name='Changed')
        self.assertTrue(p.stale(paper));paper.refresh_from_db();self.assertEqual(saved,paper.data)
        with self.assertRaises(ValidationError):p.transition(self.request,paper,'reviewed','Reviewed')

    def test_rule_change_flags_stale_without_rewrite(self):
        paper=self.paper();old=deepcopy(paper.data)
        with patch('taxation.preparation.rule_bundle',return_value={'version':'future'}):self.assertTrue(p.stale(paper))
        paper.refresh_from_db();self.assertEqual(paper.data,old)

    def test_explicit_revision_and_append_history(self):
        old=self.paper();new=self.paper(previous=old)
        self.assertEqual(new.revision,2);self.assertEqual(new.previous_id,old.pk);self.assertEqual(old.status,'superseded');self.assertEqual(old.events.count(),2)
        with self.assertRaises(ValidationError):self.paper(previous=old)

    def test_review_archive_no_filed_status(self):
        paper=self.paper();p.transition(self.request,paper,'reviewed','Owner reviewed supported evidence');self.assertEqual(paper.status,'reviewed')
        p.transition(self.request,paper,'archived','Closed working paper');self.assertEqual(paper.status,'archived')
        with self.assertRaises(ValidationError):p.transition(self.request,paper,'reviewed','Reopen')
        with self.assertRaises(ValidationError):p.transition(self.request,paper,'filed','Not supported')

    def test_archived_cannot_be_revised(self):
        paper=self.paper();p.transition(self.request,paper,'archived','Archive')
        with self.assertRaises(ValidationError):self.paper(previous=paper)

    def test_review_reason_required(self):
        with self.assertRaises(ValidationError):p.transition(self.request,self.paper(),'reviewed','')

    def test_audit_events(self):
        paper=self.paper();p.transition(self.request,paper,'reviewed','Reviewed');self.client.get(reverse('tax_paper_export',args=[paper.pk,'csv']))
        descriptions=' '.join(AuditEvent.objects.filter(module='taxation').values_list('description',flat=True))
        self.assertIn('period selected',descriptions);self.assertIn('snapshot prepared',descriptions);self.assertIn('review completed',descriptions);self.assertIn('exported',descriptions)

    def test_dashboard_period_and_preview_ui(self):
        self.assertContains(self.client.get(reverse('tax_preparation')),'Tax preparation')
        self.assertContains(self.client.get(reverse('tax_period',args=[self.period.pk])),'GSTR-1 working summary')
        url=reverse('tax_preview',args=[self.period.pk,'gstr1'])
        self.assertContains(self.client.get(url),'Review tax preparation')
        response=self.client.post(url,dict(ordinary_domestic='on',aato_band='up_to_5cr',**{f'registration_{self.invoice.pk}':'unregistered',f'reason_{self.invoice.pk}':'Reviewed'}))
        self.assertEqual(response.status_code,200);self.assertIsNotNone(response.context['preview_token']);self.assertContains(response,'224.20')

    def test_signed_preview_prepare_workflow(self):
        url=reverse('tax_preview',args=[self.period.pk,'gstr1']);data=dict(ordinary_domestic='on',aato_band='up_to_5cr',**{f'registration_{self.invoice.pk}':'unregistered',f'reason_{self.invoice.pk}':'Reviewed'})
        response=self.client.post(url,data);data.update(action='prepare',confirmed='on',preview_token=response.context['preview_token'])
        response=self.client.post(url,data);self.assertEqual(response.status_code,302);self.assertEqual(TaxWorkingPaper.objects.count(),1)
        self.assertEqual(self.client.post(url,data).status_code,302);self.assertEqual(TaxWorkingPaper.objects.count(),1)

    def test_unsigned_prepare_cannot_persist(self):
        url=reverse('tax_preview',args=[self.period.pk,'gstr1'])
        response=self.client.post(url,dict(action='prepare',confirmed='on',ordinary_domestic='on',aato_band='up_to_5cr',**{f'registration_{self.invoice.pk}':'unknown'}))
        self.assertEqual(response.status_code,200);self.assertEqual(TaxWorkingPaper.objects.count(),0)

    def test_detail_matches_snapshot(self):
        paper=self.paper();response=self.client.get(reverse('tax_paper',args=[paper.pk]));self.assertContains(response,'224.20');self.assertEqual(response.context['data'],paper.data)

    def test_cross_business_all_endpoints(self):
        paper=self.paper();self.client.force_login(self.other)
        for name,args in [('tax_period',[self.period.pk]),('tax_preview',[self.period.pk,'gstr1']),('tax_paper',[paper.pk]),('tax_paper_export',[paper.pk,'csv'])]:
            self.assertEqual(self.client.get(reverse(name,args=args)).status_code,404)
        self.assertEqual(self.client.post(reverse('tax_paper_status',args=[paper.pk]),{'status':'reviewed','reason':'Forbidden'}).status_code,404)

    def test_anonymous_redirects(self):
        self.client.logout();self.assertEqual(self.client.get(reverse('tax_preparation')).status_code,302)

    def test_csrf_review_protection(self):
        paper=self.paper();client=Client(enforce_csrf_checks=True);client.force_login(self.user)
        self.assertEqual(client.post(reverse('tax_paper_status',args=[paper.pk]),{'status':'reviewed','reason':'Forged'}).status_code,403)
        self.assertEqual(self.client.get(reverse('tax_paper_status',args=[paper.pk])).status_code,405)

    def test_unapplied_schema_gate(self):
        with patch('taxation.preparation.ready',return_value=False):
            self.assertContains(self.client.get(reverse('tax_preparation')),'migration approval is pending')
            self.assertEqual(self.client.get(reverse('tax_period',args=[self.period.pk])).status_code,302)

    def test_csv_frozen_and_injection_safe(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(buyer_name='=HYPERLINK("evil")')
        paper=self.paper();response=self.client.get(reverse('tax_paper_export',args=[paper.pk,'csv']))
        self.assertEqual(response.status_code,200);self.assertIn(b"'=HYPERLINK",response.content);self.assertIn(b'224.20',response.content)
        self.assertEqual(response['Cache-Control'],'private, no-store')

    def test_excel_frozen_injection_safe(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(buyer_name='=1+1')
        response=self.client.get(reverse('tax_paper_export',args=[self.paper().pk,'xlsx']))
        wb=load_workbook(BytesIO(response.content));cells=[cell for row in wb.active for cell in row]
        self.assertFalse(any(cell.data_type=='f' for cell in cells));self.assertTrue(any(cell.value=="'=1+1" for cell in cells));self.assertTrue(any(cell.value=='224.20' for cell in cells))

    def test_valid_a4_pdf_with_saved_totals(self):
        response=self.client.get(reverse('tax_paper_export',args=[self.paper().pk,'pdf']))
        self.assertEqual(response.status_code,200)
        with pdfplumber.open(BytesIO(response.content)) as pdf:
            self.assertGreater(len(pdf.pages),1);self.assertAlmostEqual(pdf.pages[0].width,595.28,places=1)
            text=' '.join(page.extract_text() or '' for page in pdf.pages);self.assertIn('224.20',text);self.assertIn('Working paper',text)

    def test_export_does_not_recalculate_stale_paper(self):
        paper=self.paper();Invoice.objects.filter(pk=self.invoice.pk).update(total_amount=999)
        self.assertIn(b'224.20',self.client.get(reverse('tax_paper_export',args=[paper.pk,'csv'])).content)
        paper.refresh_from_db();self.assertEqual(paper.data['included'][0]['values']['total'],'224.20')

    def test_invalid_export_format(self):
        self.assertEqual(self.client.get(reverse('tax_paper_export',args=[self.paper().pk,'json'])).status_code,400)


    def test_income_tax_preview_supported_full_year(self):
        self.period=p.create_period(self.request,'financial_year',date(2025,4,1),date(2026,3,31))
        data=p.preview(self.period,'income_tax',income_inputs())[0]
        self.assertEqual(data['income_tax']['estimated_tax'],'124800')
        self.assertIsNone(data['income_summary']['taxable_income'])

    def test_income_tax_company_is_manual_review(self):
        self.business.business_type='private_limited';self.business.save()
        self.period=p.create_period(self.request,'financial_year',date(2025,4,1),date(2026,3,31))
        data=p.preview(self.period,'income_tax',income_inputs())[0]
        self.assertIsNone(data['income_tax']['estimated_tax'])
        self.assertIn('income_manual',self.codes(data))

    def test_income_tax_source_change_does_not_recompute_snapshot(self):
        self.period=p.create_period(self.request,'financial_year',date(2025,4,1),date(2026,3,31))
        paper=self.paper('income_tax',inputs=income_inputs())
        self.business.business_type='llp';self.business.save()
        self.assertTrue(p.stale(paper));paper.refresh_from_db()
        self.assertEqual(paper.data['income_tax']['estimated_tax'],'124800')

    def test_period_create_endpoint(self):
        response=self.client.post(reverse('tax_preparation'),{'kind':'monthly','start_date':'2024-02-01','end_date':'2024-02-29'})
        self.assertEqual(response.status_code,302)
        self.assertTrue(TaxPeriod.objects.filter(business=self.business,start_date=date(2024,2,1)).exists())

    def test_period_invalid_endpoint_does_not_persist(self):
        before=TaxPeriod.objects.count()
        response=self.client.post(reverse('tax_preparation'),{'kind':'monthly','start_date':'2024-02-01','end_date':'2024-02-28'})
        self.assertContains(response,'complete calendar month');self.assertEqual(TaxPeriod.objects.count(),before)

    def test_period_queryset_cannot_modify_boundaries(self):
        with self.assertRaises(ValidationError):TaxPeriod.objects.filter(pk=self.period.pk).update(start_date=date(2026,1,1))

    def test_snapshot_foreign_actor_not_allowed(self):
        paper=self.paper();foreign=RequestFactory().post('/tax/prepare/');foreign.user=self.other
        with self.assertRaises(TaxWorkingPaper.DoesNotExist):p.transition(foreign,paper,'reviewed','Forbidden')
        with self.assertRaises(ValidationError):TaxWorkingPaperEvent.objects.create(paper=paper,actor=self.other,status='reviewed')

    def test_signed_token_bound_to_user_and_period(self):
        from django.core import signing
        from .preparation_views import SALT
        url=reverse('tax_preview',args=[self.period.pk,'gstr1'])
        token=signing.dumps({'user':self.other.pk,'period':self.period.pk,'kind':'gstr1','previous':None,'key':str(uuid.uuid4()),'digest':'x'},salt=SALT)
        response=self.client.post(url,dict(action='prepare',preview_token=token,confirmed='on',ordinary_domestic='on',aato_band='up_to_5cr',**{f'registration_{self.invoice.pk}':'unknown'}))
        self.assertContains(response,'does not belong');self.assertEqual(TaxWorkingPaper.objects.count(),0)

    def test_ui_explicit_revision(self):
        paper=self.paper();url=reverse('tax_preview',args=[self.period.pk,'gstr1'])+'?revise='+str(paper.pk)
        self.assertContains(self.client.get(url),'Explicit revision')
        self.paper(previous=paper)
        self.assertEqual(self.client.get(url).status_code,400)

    def test_duplicate_normalized_number_is_critical(self):
        self.invoice.line_items.all().delete()
        Invoice.objects.create(user=self.user,invoice_type='sales',invoice_number=self.invoice.invoice_number.lower(),invoice_date=self.period.start_date,status='verified',buyer_gstin='',taxable_amount=190,cgst_amount=D('17.10'),sgst_amount=D('17.10'),total_amount=D('224.20'))
        self.assertIn('duplicate_invoice',self.codes(self.preview()))
        with self.assertRaises(ValidationError):self.paper()

    def test_nil_exempt_non_gst_saved_classifications(self):
        for treatment in ('nil_rated','exempt','non_gst'):
            with self.subTest(treatment=treatment):
                inv=self.create(**{'items-0-tax_treatment':treatment,'items-0-tax_rate':'0','items-0-discount':'0'})
                fixtures.ManualInvoiceTests.finalize(self,inv)
        d=self.preview()
        self.assertEqual(d['gstr1']['buckets']['nil_rated']['taxable'],'200.00')
        self.assertEqual(d['gstr1']['buckets']['exempt']['taxable'],'200.00')
        self.assertEqual(d['gstr1']['buckets']['non_gst']['taxable'],'200.00')
        self.assertEqual(d['gstr3b']['original_buckets']['3.1(c)_candidate']['taxable'],'400.00')
        self.assertEqual(d['gstr3b']['original_buckets']['3.1(e)_candidate']['taxable'],'200.00')

    def test_cash_is_separate_from_revenue(self):
        InvoicePayment.objects.create(user=self.user,party=self.customer,invoice=self.invoice,payment_date=date(2026,10,2),amount=D('100'),payment_mode='cash',reference_id='test-only')
        d=self.preview('income_summary');totals=d['income_summary']['saved_analytics']['totals']
        self.assertEqual(totals['sales'],'224.20');self.assertEqual(totals['payments_received'],'100.00');self.assertEqual(totals['receivable'],'124.20')

    def test_payment_on_prior_invoice_flags_snapshot_stale(self):
        prior=self.create(invoice_date='2026-09-01',due_date='2026-09-30');fixtures.ManualInvoiceTests.finalize(self,prior)
        paper=self.paper('income_summary')
        InvoicePayment.objects.create(user=self.user,party=self.customer,invoice=prior,payment_date=date(2026,10,2),amount=D('100'),payment_mode='cash',reference_id='synthetic')
        self.assertTrue(p.stale(paper))

    def test_import_saved_rows_must_agree_with_original_file(self):
        run,_=self.imported_run();run.records.update(taxable_value=D('123'))
        self.assertEqual(self.preview(import_runs=[run.pk])['source_evidence']['imports'][0]['validation'],'source_mismatch')

    def test_import_total_tax_mismatch_disclosed(self):
        run,_=self.imported_run(total_tax=D('99'))
        self.assertIn('amount_mismatch',self.preview(import_runs=[run.pk])['reconciliation']['book_records'][0]['flags'])

    def test_xlsx_comparison_supported(self):
        from openpyxl import Workbook
        run,_=self.imported_run();record=run.records.get()
        fields=('supplier_gstin','invoice_number','invoice_date','taxable_value','cgst_amount','sgst_amount','igst_amount','total_tax')
        wb=Workbook();wb.active.append(fields);wb.active.append([getattr(record,k) for k in fields]);stream=BytesIO();wb.save(stream)
        run.source_file.save('extract.xlsx',SimpleUploadedFile('extract.xlsx',stream.getvalue()))
        self.assertEqual(self.preview(import_runs=[run.pk])['reconciliation']['book_records'][0]['status'],'matched')

    def test_direct_expense_once_no_linked_purchase_duplicate(self):
        from expenses.models import ExpenseDetail, ExpenseCategory
        inv=self.purchase();category=ExpenseCategory.objects.create(business=self.business,name='Synthetic costs');ExpenseDetail.objects.create(invoice=inv,category=category,title='Purchase attribution')
        d=self.preview('income_summary')
        self.assertEqual(len(d['included']),2);self.assertEqual(d['income_summary']['saved_analytics']['totals']['purchases'],'224.20')
        self.assertEqual(d['income_summary']['direct_expense_before_gst'],'0.00')

    def test_income_tax_form_manual_category_and_year(self):
        self.period=p.create_period(self.request,'financial_year',date(2025,4,1),date(2026,3,31))
        url=reverse('tax_preview',args=[self.period.pk,'income_tax'])
        response=self.client.post(url,dict(aato_band='unknown',**income_inputs()))
        self.assertEqual(response.status_code,200);self.assertContains(response,'124800');self.assertIsNotNone(response.context['preview_token'])

    def test_local_date_period_warning_respects_timezone(self):
        with timezone.override('Asia/Kolkata'), patch('django.utils.timezone.now',return_value=timezone.datetime(2026,9,30,20,0,tzinfo=timezone.utc) if hasattr(timezone,'utc') else timezone.datetime.fromisoformat('2026-09-30T20:00:00+00:00')):
            self.assertIn('open_period',self.codes(self.preview()))

    def test_review_is_idempotent_no_duplicate_events(self):
        paper=self.paper();p.transition(self.request,paper,'reviewed','Reviewed');p.transition(self.request,paper,'reviewed','Reviewed')
        self.assertEqual(paper.events.count(),2)

    def test_resource_limit_blocks_oversize_source_period(self):
        with patch('django.db.models.query.QuerySet.count',return_value=2001):
            with self.assertRaises(ValidationError):self.preview()

    def test_saved_components_conflicting_with_states_block(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(place_of_supply='29')
        self.assertIn('component_supply_mismatch',self.codes(self.preview()))
        with self.assertRaises(ValidationError):self.paper()

    def test_pdf_is_summary_full_evidence_remains_in_csv(self):
        response=self.client.get(reverse('tax_paper_export',args=[self.paper().pk,'pdf']))
        with pdfplumber.open(BytesIO(response.content)) as pdf:
            text=' '.join(page.extract_text() or '' for page in pdf.pages)
            self.assertIn('Complete frozen source/rule evidence',text)

    def test_invalid_revision_identifier_is_404(self):
        url=reverse('tax_preview',args=[self.period.pk,'gstr1'])+'?revise=invalid'
        self.assertEqual(self.client.get(url).status_code,404)

    def test_audit_export_contains_scoped_preparation_review_and_prior_export(self):
        paper=self.paper();p.transition(self.request,paper,'reviewed','Owner checked')
        self.client.get(reverse('tax_paper_export',args=[paper.pk,'csv']))
        response=self.client.get(reverse('tax_paper_export',args=[paper.pk,'csv']))
        self.assertIn(b'audit_at_export',response.content);self.assertIn(b'snapshot prepared',response.content);self.assertIn(b'review completed',response.content);self.assertIn(b'exported',response.content)
        self.assertTrue(set(AuditEvent.objects.filter(module='taxation').values_list('action',flat=True)) <= set(dict(AuditEvent.ACTION_CHOICES)))

    def test_foreign_audit_events_excluded_from_export(self):
        paper=self.paper()
        AuditEvent.objects.create(user=self.other,business=self.other_business,module='taxation',action='create',object_type='taxation.taxworkingpaper',object_id=str(paper.pk),description='FOREIGN_AUDIT_MUST_NOT_LEAK')
        response=self.client.get(reverse('tax_paper_export',args=[paper.pk,'csv']))
        self.assertNotIn(b'FOREIGN_AUDIT_MUST_NOT_LEAK',response.content)

    def test_sales_issuer_registration_mismatch_blocks_gst_only(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(supplier_gstin=valid_gstin('29ABCDE1234F1Z'))
        self.assertIn('issuer_gstin_mismatch',self.codes(self.preview()))
        with self.assertRaises(ValidationError):self.paper()
        self.assertNotIn('issuer_gstin_mismatch',self.codes(self.preview('income_summary')))

    def test_missing_issuer_and_foreign_input_buyer_disclosed(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(supplier_gstin='')
        purchase=self.purchase();Invoice.objects.filter(pk=purchase.pk).update(buyer_gstin=valid_gstin('29ABCDE1234F1Z'))
        codes=self.codes(self.preview());self.assertIn('issuer_gstin_missing',codes);self.assertIn('input_buyer_gstin_mismatch',codes)


class AdjustmentPreparationTests(adjustments.AdjustmentHelpers,TestCase):
    def setUp(self):
        self.setup_data();self.business.gstin=valid_gstin();self.business.save()
        self.period=p.create_period(self.request,'financial_year',date(2026,4,1),date(2027,3,31))
        self.inputs=dict(ordinary_domestic=True,aato_band='up_to_5cr',registration={str(self.invoice.pk):{'status':'unregistered','reason':'Reviewed'}})
    def data(self):return p.preview(self.period,'gstr3b',self.inputs)[0]
    def test_credit_reduces_saved_output(self):
        self.finalize_note(self.create_note());d=self.data();self.assertEqual(d['liability']['net_output']['cgst'],'720.00');self.assertEqual(d['gstr1']['net_gst_adjusted_outward']['total'],'9440.00')
    def test_debit_increases_output(self):
        self.finalize_note(self.create_note('debit'));self.assertEqual(self.data()['liability']['net_output']['cgst'],'1080.00')
    def test_cancelled_note_reversal_once(self):
        note=self.create_note();self.finalize_note(note);self.cancel_note(note)
        d=self.data();self.assertEqual(d['liability']['net_output']['cgst'],'900.00');self.assertEqual(len(d['adjustment_events']),2)
    def test_draft_notes_not_recognized(self):
        self.create_note();self.assertEqual(self.data()['adjustment_events'],[])
    def test_prior_invoice_note_in_current_period(self):
        self.finalize_note(self.create_note());month=timezone.localdate().replace(day=1);end=date(month.year,month.month,calendar.monthrange(month.year,month.month)[1]);self.period=p.create_period(self.request,'monthly',month,end);self.inputs['registration']={}
        d=self.data();self.assertEqual(d['liability']['net_output']['cgst'],'-180.00');self.assertIn('negative_output',{w['code'] for w in d['warnings']})
    def test_financial_only_does_not_adjust_gst(self):
        self.finalize_note(self.create_note(tax_effect='financial'));d=self.data();self.assertEqual(d['liability']['net_output']['cgst'],'900.00');self.assertEqual(d['adjustment_events'][0]['values']['cgst'],'0.00')

    def test_multiple_notes_accumulate_once(self):
        self.finalize_note(self.create_note());self.finalize_note(self.create_note('debit'))
        self.assertEqual(self.data()['liability']['net_output']['cgst'],'900.00')
        self.assertEqual(len(self.data()['adjustment_events']),2)

    def test_purchase_note_adjustment_disclosed_separately(self):
        purchase=fixtures.ManualInvoiceTests.create(self,'purchase',**{'items-0-quantity':'10','items-0-rate':'1000','items-0-discount':'0'})
        fixtures.ManualInvoiceTests.finalize(self,purchase);purchase.refresh_from_db()
        self.finalize_note(self.create_note(invoice=purchase))
        d=self.data();self.assertEqual(d['gstr3b']['input_adjustments']['cgst'],'-180.00')
        self.assertEqual(d['liability']['reviewed_input']['cgst'],'0.00')
