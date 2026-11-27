from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
import smtplib,uuid
from unittest.mock import patch
from django.test import TestCase,Client,RequestFactory,override_settings,TransactionTestCase
from django.core.exceptions import ValidationError
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from django.db import transaction,IntegrityError
import pdfplumber
from . import test_manual as fixtures
from .models import Invoice,InvoicePayment,InvoiceAdjustmentNote as Note,InvoiceAdjustmentLine as Line,AdjustmentNoteSequence,AdjustmentNoteEmailDelivery as History,InvoiceNumberSequence
from .adjustment_services import save_adjustment,transition_adjustment
from .adjustment_reporting import adjusted_invoice_amount,overpayment,note_events
from .adjustment_pdf import generate_adjustment_pdf
from .ledger import invoice_outstanding,party_ledger_entries
from .email_delivery import send_adjustment_email
from analytics.services import build_analytics_context
from audit.models import AuditEvent
D=Decimal


class AdjustmentHelpers:
    def setup_data(self):
        fixtures.ManualInvoiceTests.setUp(self)
        self.customer.email='customer@example.test';self.customer.save()
        self.invoice=fixtures.ManualInvoiceTests.create(self,**{'items-0-quantity':'10','items-0-rate':'1000','items-0-discount':'0'})
        fixtures.ManualInvoiceTests.finalize(self,self.invoice);self.invoice.refresh_from_db()
        self.source=self.invoice.line_items.get();self.request=RequestFactory().post('/adjustments/');self.request.user=self.user
    def payload(self,*args,**kwargs):return fixtures.ManualInvoiceTests.payload(self,*args,**kwargs)
    def create_note(self,kind='credit',invoice=None,rows=None,**updates):
        invoice=invoice or self.invoice
        data={'document_date':timezone.localdate(),'reason':'Synthetic correction','pricing_mode':'exclusive','tax_effect':'gst','supplier_reference':'SUP-CN-1','eligibility_reviewed':True,'annual_return_date':None,'invoice_discount_type':'fixed','invoice_discount_value':D('0'),'notes':''}
        data.update(updates)
        rows=rows if rows is not None else [{'original_line':invoice.line_items.first(),'mode':'value','quantity':D('0'),'amount':D('2000')}]
        return save_adjustment(request=self.request,invoice_id=invoice.pk,kind=kind,data=data,rows=rows,creation_key=uuid.uuid4())
    def finalize_note(self,note):
        result=transition_adjustment(request=self.request,note_id=note.pk,action='finalize');note.refresh_from_db();return result
    def cancel_note(self,note,day=None):
        result=transition_adjustment(request=self.request,note_id=note.pk,action='cancel',reason='Correction reversal',cancellation_date=day);note.refresh_from_db();return result
    def totals(self,start=None,end=None):return build_analytics_context(self.user,start or date(2026,10,1),end or timezone.localdate(),'custom')['totals']
    def send(self,note,key=None,**kwargs):return send_adjustment_email(user=self.user,document_id=note.pk,recipient='customer@example.test',request_key=key or uuid.uuid4(),request=self.request,**kwargs)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AdjustmentTests(AdjustmentHelpers,TestCase):
    def setUp(self):self.setup_data()
    def test_partial_credit_and_required_payment_example(self):
        InvoicePayment.objects.create(user=self.user,party=self.customer,invoice=self.invoice,payment_date=timezone.localdate(),amount=D('5000'),payment_mode='bank_transfer',reference_id='real-test-fixture')
        note=self.create_note();self.finalize_note(note)
        self.assertEqual(note.total_amount,D('2360'));self.assertEqual(invoice_outstanding(self.invoice),D('4440'))
        self.assertEqual(self.totals()['sales'],D('9440'));self.assertEqual(self.totals()['output_gst'],D('1440'))
    def test_drafts_do_not_affect_reports_or_balances(self):
        before=self.totals();self.create_note();self.assertEqual(before,self.totals());self.assertEqual(adjusted_invoice_amount(self.invoice),D('11800'))
    def test_full_credit_and_repeated_credits_capped(self):
        first=self.create_note();self.finalize_note(first);second=self.create_note(rows=[{'original_line':self.source,'mode':'value','quantity':0,'amount':D('8000')}]);self.finalize_note(second)
        self.assertEqual(invoice_outstanding(self.invoice),0)
        with self.assertRaises(ValidationError):self.create_note()
    def test_quantity_and_value_credits_share_limits(self):
        q=self.create_note(rows=[{'original_line':self.source,'mode':'quantity','quantity':D('2'),'amount':0}]);self.finalize_note(q)
        self.assertEqual(q.total_amount,D('2360'))
        with self.assertRaises(ValidationError):self.create_note(rows=[{'original_line':self.source,'mode':'quantity','quantity':D('9'),'amount':0}])
        value=self.create_note(rows=[{'original_line':self.source,'mode':'value','quantity':0,'amount':D('8000')}]);self.finalize_note(value)
        with self.assertRaises(ValidationError):self.create_note(rows=[{'original_line':self.source,'mode':'quantity','quantity':D('1'),'amount':0}])
    def test_debit_does_not_replenish_credit_budget(self):
        debit=self.create_note('debit');self.finalize_note(debit);credit=self.create_note(rows=[{'original_line':self.source,'mode':'value','quantity':0,'amount':D('10000')}]);self.finalize_note(credit)
        with self.assertRaises(ValidationError):self.create_note()
        self.assertEqual(adjusted_invoice_amount(self.invoice),D('2360'))
    def test_inclusive_decimal_rounding_and_full_residual(self):
        note=self.create_note(pricing_mode='inclusive',rows=[{'original_line':self.source,'mode':'value','quantity':0,'amount':D('1180.03')}]);self.finalize_note(note)
        self.assertEqual(note.total_amount,D('1180.03'));self.assertEqual(note.taxable_amount,D('1000.03'))
        remaining=self.create_note(rows=[{'original_line':self.source,'mode':'value','quantity':0,'amount':D('8999.97')}]);self.finalize_note(remaining)
        self.assertEqual(note.cgst_amount+remaining.cgst_amount,self.invoice.cgst_amount)
        self.assertEqual(adjusted_invoice_amount(self.invoice),0)
    def test_debit_inclusive_discount_and_quantity_increase(self):
        note=self.create_note('debit',pricing_mode='inclusive',invoice_discount_type='percent',invoice_discount_value=D('10'),rows=[{'original_line':self.source,'mode':'quantity','quantity':D('2'),'amount':D('1180'),'discount':D('10'),'discount_type':'percent'}]);self.finalize_note(note)
        self.assertEqual(note.total_amount,D('1911.60'));self.assertEqual(note.invoice_discount_amount,D('212.40'))
    def test_financial_only_has_no_gst_effect(self):
        before=self.totals();note=self.create_note(tax_effect='financial',eligibility_reviewed=False);self.finalize_note(note)
        self.assertEqual(note.total_amount,D('2000'));self.assertEqual(self.totals()['output_gst'],before['output_gst'])
        with self.assertRaises(ValidationError):self.create_note()
    def test_gst_requires_review_deadline_and_annual_return(self):
        note=self.create_note(eligibility_reviewed=False)
        with self.assertRaises(ValidationError):self.finalize_note(note)
        note.eligibility_reviewed=True;note.annual_return_date=note.document_date;note.save()
        with self.assertRaises(ValidationError):self.finalize_note(note)
    def test_no_original_or_payment_mutation_and_finalized_immutable(self):
        original=Invoice.objects.filter(pk=self.invoice.pk).values().get();lines=list(self.invoice.line_items.values());note=self.create_note();self.finalize_note(note)
        self.assertEqual(Invoice.objects.filter(pk=self.invoice.pk).values().get(),original);self.assertEqual(list(self.invoice.line_items.values()),lines);self.assertFalse(InvoicePayment.objects.exists())
        note.total_amount=D('1')
        with self.assertRaises(ValidationError):note.save()
        line=note.line_items.first();line.rate=1
        with self.assertRaises(ValidationError):line.save()
    def test_finalization_idempotent(self):
        note=self.create_note();self.finalize_note(note);self.finalize_note(note)
        self.assertEqual(len(note_events(self.user)),1);self.assertEqual(AuditEvent.objects.filter(module='adjustments',action='verify').count(),1)
    def test_cancelled_finalized_note_retains_dated_recognition(self):
        note=self.create_note(document_date=date(2026,10,2));self.finalize_note(note);self.cancel_note(note)
        self.assertEqual(self.totals(date(2026,10,1),date(2026,10,31))['sales'],D('9440'))
        self.assertEqual(self.totals()['sales'],D('11800'));self.assertEqual(len(note_events(self.user)),2);self.assertEqual(adjusted_invoice_amount(self.invoice),D('11800'))
    def test_draft_cancel_no_impact_reason_required(self):
        note=self.create_note()
        with self.assertRaises(ValidationError):transition_adjustment(request=self.request,note_id=note.pk,action='cancel')
        self.cancel_note(note);self.assertEqual(note_events(self.user),[])
        with self.assertRaises(ValidationError):self.finalize_note(note)
    def test_backdating_and_future_dates_rejected(self):
        with self.assertRaises(ValidationError):self.create_note(document_date=timezone.localdate()+timedelta(days=1))
        first=self.create_note();self.finalize_note(first);older=self.create_note(document_date=date(2026,10,2))
        with self.assertRaises(ValidationError):self.finalize_note(older)
    def test_overpayment_not_negative_receivable_or_fake_refund(self):
        InvoicePayment.objects.create(user=self.user,party=self.customer,invoice=self.invoice,payment_date=timezone.localdate(),amount=D('11800'),payment_mode='bank_transfer',reference_id='paid-fixture')
        self.invoice.status='paid';self.invoice.save();note=self.create_note();self.finalize_note(note)
        self.assertEqual(invoice_outstanding(self.invoice),0);self.assertEqual(overpayment(self.invoice),D('2360'));self.assertEqual(InvoicePayment.objects.count(),1)
    def test_debit_reopens_paid_invoice_balance_without_status_rewrite(self):
        InvoicePayment.objects.create(user=self.user,party=self.customer,invoice=self.invoice,payment_date=timezone.localdate(),amount=D('11800'),payment_mode='bank_transfer',reference_id='paid-fixture')
        self.invoice.status='paid';self.invoice.save();note=self.create_note('debit');self.finalize_note(note)
        self.assertEqual(invoice_outstanding(self.invoice),D('2360'));self.invoice.refresh_from_db();self.assertEqual(self.invoice.status,'paid')
    def test_independent_numbers_and_original_sequence_unchanged(self):
        before=list(InvoiceNumberSequence.objects.values());credit=self.create_note();debit=self.create_note('debit')
        self.assertTrue(credit.number.startswith('CN/'));self.assertTrue(debit.number.startswith('DN/'));self.assertLessEqual(len(credit.number),16);self.assertEqual(list(InvoiceNumberSequence.objects.values()),before)
    def test_purchase_notes_reverse_purchase_and_input_tax(self):
        purchase=fixtures.ManualInvoiceTests.create(self,'purchase',**{'items-0-quantity':'10','items-0-rate':'1000','items-0-discount':'0'})
        fixtures.ManualInvoiceTests.finalize(self,purchase);purchase.refresh_from_db();note=self.create_note(invoice=purchase);self.finalize_note(note)
        self.assertEqual(note.direction,'received');self.assertTrue(note.number.startswith('RC/'));self.assertEqual(self.totals()['purchases'],D('9440'));self.assertEqual(self.totals()['input_gst'],D('1440'))
        debit=self.create_note('debit',invoice=purchase,supplier_reference='SUP-DN-1');self.finalize_note(debit);self.assertEqual(self.totals()['purchases'],D('11800'))
    def test_invalid_sources_cross_business_and_negative_inputs(self):
        for updates in [{'status':'cancelled','document_state':'cancelled'},{'document_state':'draft','status':'draft'},{'calculation_version':None}]:
            old={k:getattr(self.invoice,k) for k in updates};Invoice.objects.filter(pk=self.invoice.pk).update(**updates)
            with self.assertRaises(ValidationError):self.create_note()
            Invoice.objects.filter(pk=self.invoice.pk).update(**old)
        with self.assertRaises(ValidationError):self.create_note(rows=[{'original_line':self.source,'mode':'value','quantity':0,'amount':D('-1')}])
        self.request.user=self.other
        with self.assertRaises(Invoice.DoesNotExist):self.create_note()
    def test_ledger_records_note_and_reversal(self):
        note=self.create_note();self.finalize_note(note);entries=party_ledger_entries(self.user,self.customer);self.assertEqual(entries[-1]['balance'],D('9440'));self.cancel_note(note);self.assertEqual(party_ledger_entries(self.user,self.customer)[-1]['balance'],D('11800'))
    def test_pdf_saved_values_titles_references_watermarks_and_download(self):
        for kind,label in [('credit','CREDIT NOTE'),('debit','DEBIT NOTE')]:
            note=self.create_note(kind);content=generate_adjustment_pdf(note)
            with pdfplumber.open(BytesIO(content)) as pdf:
                text='\n'.join(page.extract_text() or '' for page in pdf.pages);self.assertIn(label,text);self.assertIn(self.invoice.invoice_number,text);self.assertIn('DRAFT',text)
            self.finalize_note(note);self.assertEqual(self.client.get(reverse('adjustment_pdf',args=[note.pk])).status_code,200);self.cancel_note(note)
            with pdfplumber.open(BytesIO(generate_adjustment_pdf(note))) as pdf:self.assertIn('CANCELLED','\n'.join(p.extract_text() or '' for p in pdf.pages))
    def test_email_locmem_pdf_history_and_duplicate(self):
        note=self.create_note();self.finalize_note(note);key=uuid.uuid4();first=self.send(note,key);second=self.send(note,key)
        self.assertEqual(first.status,'sent');self.assertTrue(second.reused);self.assertEqual(History.objects.count(),1);self.assertEqual(len(mail.outbox),1)
        message=mail.outbox[0];self.assertIn(note.number,message.subject);self.assertIn('Credit Note',message.subject);self.assertEqual(message.to,['customer@example.test']);self.assertEqual(message.attachments[0].content,generate_adjustment_pdf(note));self.assertIn(self.invoice.invoice_number,message.body);self.assertTrue(message.alternatives)
    def test_email_draft_and_cross_owner_rejected(self):
        note=self.create_note()
        with self.assertRaises(ValidationError):self.send(note)
        self.finalize_note(note)
        with self.assertRaises(ValidationError):send_adjustment_email(user=self.other,document_id=note.pk,recipient='customer@example.test',request_key=uuid.uuid4())
    def test_email_failure_retry_and_uncertain_no_automatic_resend(self):
        note=self.create_note();self.finalize_note(note)
        with patch('django.core.mail.backends.locmem.EmailBackend.send_messages',side_effect=smtplib.SMTPAuthenticationError(535,b'not logged')):failed=self.send(note)
        self.assertEqual(failed.error_category,'authentication_failed')
        History.objects.filter(pk=failed.delivery_id).update(created_at=timezone.now()-timedelta(minutes=2))
        retried=self.send(note,retry_id=failed.delivery_id);self.assertEqual(retried.status,'sent');self.assertEqual(History.objects.count(),2)
    def test_business_authorization_and_post_csrf(self):
        note=self.create_note();self.client.force_login(self.other)
        for name in ['adjustment_detail','adjustment_edit','adjustment_pdf','adjustment_email']:
            self.assertEqual(self.client.get(reverse(name,args=[note.pk])).status_code,404)
        self.client.force_login(self.user);self.assertEqual(self.client.get(reverse('adjustment_transition',args=[note.pk,'finalize'])).status_code,405)
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.user);self.assertEqual(csrf.post(reverse('adjustment_transition',args=[note.pk,'finalize']),{'confirm':'on'}).status_code,403)
        self.assertEqual(Client().get(reverse('adjustment_detail',args=[note.pk])).status_code,302)
    def test_report_exports_and_date_period(self):
        note=self.create_note();self.finalize_note(note)
        for extension in [None,'csv','xlsx']:
            response=self.client.get(reverse('adjustment_report'),{'export':extension or ''});self.assertEqual(response.status_code,200)
            if extension=='csv':self.assertIn(note.number,response.content.decode())
        self.assertEqual(self.client.get(reverse('adjustment_report'),{'start':'bad'}).status_code,400)
    def test_missing_schema_gate(self):
        with patch('invoices.adjustment_views.adjustments_available',return_value=False):self.assertEqual(self.client.get(reverse('adjustment_create')).status_code,503)

    def test_server_preview_signed_creation_edit_and_form_workflow(self):
        page=self.client.get(reverse('adjustment_create'),{'invoice':self.invoice.pk});self.assertEqual(page.status_code,200)
        data={'creation_token':page.context['form'].initial['creation_token'],'original_invoice':self.invoice.pk,'kind':'credit','document_date':timezone.localdate().isoformat(),'reason':'UI correction','tax_effect':'gst','pricing_mode':'exclusive','supplier_reference':'','invoice_discount_type':'fixed','invoice_discount_value':'0','eligibility_reviewed':'on','annual_return_date':'','notes':'',
            'items-TOTAL_FORMS':'1','items-INITIAL_FORMS':'0','items-MIN_NUM_FORMS':'1','items-MAX_NUM_FORMS':'100','items-0-original_line':self.source.pk,'items-0-mode':'value','items-0-quantity':'0','items-0-amount':'2000','items-0-discount':'0','items-0-discount_type':'fixed'}
        preview=self.client.post(reverse('adjustment_create'),{**data,'preview':'1'});self.assertEqual(preview.status_code,200);self.assertContains(preview,'2360.00');self.assertFalse(Note.objects.exists());self.assertFalse(AdjustmentNoteSequence.objects.exists())
        response=self.client.post(reverse('adjustment_create'),data);self.assertEqual(response.status_code,302);self.assertEqual(Note.objects.count(),1)
        replay=self.client.post(reverse('adjustment_create'),data);self.assertEqual(response.url,replay.url);self.assertEqual(Note.objects.count(),1)
        note=Note.objects.get();edit=self.client.get(reverse('adjustment_edit',args=[note.pk]));data.update(creation_token=edit.context['form'].initial['creation_token'],notes='Only notes edited',**{'items-INITIAL_FORMS':'1'})
        self.assertEqual(self.client.post(reverse('adjustment_edit',args=[note.pk]),data).status_code,302);note.refresh_from_db();self.assertEqual(note.total_amount,D('2360'))
        self.assertEqual(self.client.get(reverse('adjustment_detail',args=[note.pk])).status_code,200)
        self.client.post(reverse('adjustment_transition',args=[note.pk,'finalize']),{'confirm':'on'});note.refresh_from_db();self.assertEqual(note.status,'finalized')
        self.assertEqual(self.client.get(reverse('adjustment_edit',args=[note.pk])).status_code,400)
    def test_email_confirmation_signed_token_and_scope(self):
        note=self.create_note();self.finalize_note(note);page=self.client.get(reverse('adjustment_email',args=[note.pk]));self.assertEqual(page.status_code,200)
        data={'send_token':page.context['form'].initial['send_token'],'recipient':'customer@example.test','confirm':'on'}
        self.assertEqual(self.client.post(reverse('adjustment_email',args=[note.pk]),data).status_code,302);self.assertEqual(self.client.post(reverse('adjustment_email',args=[note.pk]),data).status_code,302);self.assertEqual(History.objects.count(),1)
        other=self.create_note('debit');self.finalize_note(other);self.assertEqual(self.client.post(reverse('adjustment_email',args=[other.pk]),data).status_code,200);self.assertEqual(History.objects.count(),1)
    def test_remaining_credit_checked_again_on_finalization(self):
        a=self.create_note(rows=[{'original_line':self.source,'mode':'value','quantity':0,'amount':D('6000')}]);b=self.create_note(rows=[{'original_line':self.source,'mode':'value','quantity':0,'amount':D('6000')}]);self.finalize_note(a)
        with self.assertRaises(ValidationError):self.finalize_note(b)
        b.refresh_from_db();self.assertEqual(b.status,'draft');self.assertEqual(len(note_events(self.user)),1)
    def test_mixed_rates_and_saved_discount_allocations(self):
        inv=fixtures.ManualInvoiceTests.create(self,**{'items-TOTAL_FORMS':'2','items-0-quantity':'1','items-0-rate':'1000','items-0-discount':'100','invoice_discount_type':'percent','invoice_discount_value':'10',
            'items-1-description':'Five percent','items-1-hsn_sac_code':'9983','items-1-quantity':'1','items-1-unit':'Nos','items-1-rate':'1000','items-1-discount':'0','items-1-tax_rate':'5','items-1-discount_type':'fixed','items-1-tax_treatment':'taxable'})
        fixtures.ManualInvoiceTests.finalize(self,inv);inv.refresh_from_db()
        note=self.create_note(invoice=inv,rows=[{'original_line':line,'mode':'quantity','quantity':line.quantity,'amount':0} for line in inv.line_items.all()]);self.finalize_note(note)
        self.assertEqual(note.total_amount,inv.total_amount);self.assertEqual(note.cgst_amount,inv.cgst_amount);self.assertEqual(adjusted_invoice_amount(inv),0)
    def test_interstate_and_non_gst_classification_saved(self):
        inv=fixtures.ManualInvoiceTests.create(self,**{'place_of_supply':'29','items-0-discount':'0'});fixtures.ManualInvoiceTests.finalize(self,inv);inv.refresh_from_db()
        note=self.create_note(invoice=inv,rows=[{'original_line':inv.line_items.first(),'mode':'value','quantity':0,'amount':D('100')}]);self.finalize_note(note);self.assertEqual(note.igst_amount,D('18'));self.assertEqual(note.cgst_amount,0)
        for treatment in ['exempt','nil_rated','non_gst']:
            other=fixtures.ManualInvoiceTests.create(self,**{'items-0-tax_treatment':treatment,'items-0-tax_rate':'0','items-0-discount':'0'});fixtures.ManualInvoiceTests.finalize(self,other);other.refresh_from_db()
            credit=self.create_note(invoice=other,rows=[{'original_line':other.line_items.first(),'mode':'value','quantity':0,'amount':D('100')}]);self.finalize_note(credit);self.assertEqual(credit.cgst_amount,0);self.assertEqual(credit.line_items.get().tax_treatment,treatment)
    def test_original_invoice_cancel_blocked_after_note_recognition(self):
        note=self.create_note();self.finalize_note(note);self.client.post(reverse('invoice_delete',args=[self.invoice.pk]));self.invoice.refresh_from_db();self.assertEqual(self.invoice.status,'verified')
    def test_note_and_supplier_reference_uniqueness(self):
        note=self.create_note()
        with self.assertRaises(IntegrityError),transaction.atomic():Note.objects.filter(pk=note.pk).update(kind='invalid')
        with self.assertRaises(IntegrityError),transaction.atomic():Note.objects.filter(pk=note.pk).update(cgst_amount=D('-1'))
    def test_email_uncertain_cooldown_and_business_quota(self):
        note=self.create_note();self.finalize_note(note)
        with patch('django.core.mail.backends.locmem.EmailBackend.send_messages',side_effect=TimeoutError):result=self.send(note)
        self.assertEqual(result.status,'uncertain');self.assertEqual(History.objects.count(),1)
        with self.assertRaises(ValidationError):self.send(note)
        another=self.create_note('debit');self.finalize_note(another)
        with patch('invoices.email_delivery.HOURLY_BUSINESS_LIMIT',1):
            with self.assertRaises(ValidationError):self.send(another)
    def test_multipage_pdf_preserves_saved_lines(self):
        note=self.create_note();line=note.line_items.get();line.description='Long description '*28;line.save()
        # Real distinct original lines are required; fixtures use bulk rows only for renderer stress.
        original=self.source
        from .models import InvoiceLineItem
        for index in range(30):
            source=InvoiceLineItem.objects.create(invoice=self.invoice,description='Stress line',quantity=1,rate=1,tax_rate=0,taxable_amount=1,tax_amount=0,total_amount=1)
            values={f.name:getattr(line,f.name) for f in Line._meta.fields if f.name not in {'id','document','original_line'}}
            Line.objects.create(document=note,original_line=source,**values)
        with pdfplumber.open(BytesIO(generate_adjustment_pdf(note))) as pdf:
            self.assertGreater(len(pdf.pages),2);self.assertTrue(all(abs(p.width-595.28)<1 for p in pdf.pages));self.assertIn('CREDIT NOTE',pdf.pages[0].extract_text())
            self.assertIn('Long description', pdf.pages[0].extract_text())

    def test_backdated_credit_cannot_bypass_current_gst_deadline(self):
        source=fixtures.ManualInvoiceTests.create(self,**{'invoice_date':'2024-10-01','due_date':'2024-10-31','items-0-rate':'1000','items-0-discount':'0'})
        fixtures.ManualInvoiceTests.finalize(self,source);source.refresh_from_db()
        note=self.create_note(invoice=source,document_date=date(2025,10,1),rows=[{'original_line':source.line_items.first(),'mode':'value','quantity':0,'amount':D('100')}])
        with self.assertRaises(ValidationError):self.finalize_note(note)
    def test_payment_entry_uses_adjusted_limit_then_reopened_paid_balance(self):
        note=self.create_note();self.finalize_note(note)
        data={'invoice':self.invoice.pk,'payment_date':timezone.localdate().isoformat(),'amount':'10000','payment_mode':'cash','reference_id':'adjusted-limit','notes':''}
        self.client.post(reverse('party_detail',args=[self.customer.pk]),data);self.assertFalse(InvoicePayment.objects.exists())
        data['amount']='9440';self.assertEqual(self.client.post(reverse('party_detail',args=[self.customer.pk]),data).status_code,302);self.assertEqual(InvoicePayment.objects.count(),1)
        debit=self.create_note('debit');self.finalize_note(debit);self.invoice.refresh_from_db();self.assertEqual(self.invoice.status,'paid');self.assertEqual(invoice_outstanding(self.invoice),D('2360'))
        data.update(amount='2360',reference_id='adjusted-debit-paid');self.assertEqual(self.client.post(reverse('party_detail',args=[self.customer.pk]),data).status_code,302);self.assertEqual(InvoicePayment.objects.count(),2)
    def test_note_period_revenue_and_balance_when_original_outside_period(self):
        note=self.create_note(document_date=timezone.localdate());self.finalize_note(note)
        totals=self.totals(timezone.localdate().replace(day=1),timezone.localdate());self.assertEqual(totals['sales'],D('-2360'));self.assertEqual(totals['output_gst'],D('-360'));self.assertEqual(totals['receivable'],D('9440'))
        historical=self.totals(date(2026,10,1),date(2026,10,31));self.assertEqual(historical['sales'],D('11800'));self.assertEqual(historical['receivable'],D('11800'))
    def test_report_pdf_and_export_formula_safety(self):
        note=self.create_note();self.finalize_note(note)
        response=self.client.get(reverse('adjustment_report'),{'export':'pdf'});self.assertEqual(response.status_code,200)
        with pdfplumber.open(BytesIO(response.content)) as pdf:self.assertIn(note.number,'\n'.join(p.extract_text() or '' for p in pdf.pages))
        from openpyxl import load_workbook
        response=self.client.get(reverse('adjustment_report'),{'export':'xlsx'});book=load_workbook(BytesIO(response.content));self.assertEqual(book.active.cell(2,12).value,-2360);self.assertIn('Financial summary',[row[0] for row in book.active.iter_rows(values_only=True)])
    def test_incomplete_saved_tax_details_block_credit_and_debit(self):
        self.source.cgst_amount=None;self.source.save()
        for kind in ['credit','debit']:
            with self.assertRaises(ValidationError):self.create_note(kind)
    def test_finalized_invoice_pdf_includes_adjusted_balance_without_queries(self):
        note=self.create_note();self.finalize_note(note)
        from .pdf_generator import invoice_pdf_data
        invoice=Invoice.objects.prefetch_related('line_items','payments','adjustment_notes').get(pk=self.invoice.pk)
        with self.assertNumQueries(0):data=invoice_pdf_data(invoice)
        self.assertEqual(data['payment']['adjusted_total'],D('9440'));self.assertEqual(data['payment']['outstanding'],D('9440'));self.assertEqual(data['totals']['total_amount'],D('11800'))


    def test_debit_quantity_requires_explicit_rate_and_respects_inclusive_source(self):
        with self.assertRaises(ValidationError):self.create_note('debit',rows=[{'original_line':self.source,'mode':'quantity','quantity':D('1'),'amount':0}])
        note=self.create_note('debit',pricing_mode='inclusive',rows=[{'original_line':self.source,'mode':'quantity','quantity':D('1'),'amount':D('1180')}]);self.assertEqual(note.total_amount,D('1180'));self.assertEqual(note.taxable_amount,D('1000'))


class AdjustmentConcurrencyTests(AdjustmentHelpers,TransactionTestCase):
    def setUp(self):self.setup_data()
    def test_concurrent_finalization_cannot_overcredit(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections,OperationalError
        notes=[self.create_note(rows=[{'original_line':self.source,'mode':'value','quantity':0,'amount':D('6000')}]) for _ in range(2)]
        barrier=Barrier(2)
        def finalize(pk):
            close_old_connections()
            try:
                barrier.wait(timeout=10);transition_adjustment(request=self.request,note_id=pk,action='finalize');return True
            except (ValidationError,OperationalError):return False
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(finalize,[n.pk for n in notes]))
        self.assertLessEqual(Note.objects.filter(status='finalized').count(),1)
        # A lock conflict is surfaced to the caller; an explicit later action is safe.
        if not any(results):self.finalize_note(notes[0])
        self.assertEqual(Note.objects.filter(status='finalized').count(),1);self.assertEqual(adjusted_invoice_amount(self.invoice),D('4720'))
    def test_concurrent_number_allocation_is_unique(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections,OperationalError
        barrier=Barrier(2)
        def create(index):
            close_old_connections()
            try:barrier.wait(timeout=10);return self.create_note().pk
            except OperationalError:return None
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(create,[0,1]))
        if not any(results):self.create_note()
        numbers=list(Note.objects.values_list('number',flat=True));self.assertEqual(len(numbers),len(set(numbers)))
