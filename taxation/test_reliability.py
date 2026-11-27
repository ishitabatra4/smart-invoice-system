from datetime import date
from decimal import Decimal
from django.test import TestCase, override_settings, Client
from django.urls import reverse
from django.http import QueryDict
from . import test_preparation as fixtures
from .models import TaxWorkingPaper
from invoices.models import Invoice


class RequestReliabilityTests(TestCase):
    setUp = fixtures.PreparationTests.setUp
    create = fixtures.PreparationTests.create
    payload = fixtures.PreparationTests.payload
    inputs = fixtures.PreparationTests.inputs
    paper = fixtures.PreparationTests.paper

    def scale(self, count):
        Invoice.objects.bulk_create([Invoice(user=self.user,invoice_type='sales',invoice_number=f'SCALE-{i}',invoice_date=date(2026,10,1),supplier_name='Synthetic',supplier_gstin=self.business.gstin,status='verified') for i in range(count-1)])

    def body(self):
        data=QueryDict('',mutable=True)
        data.update({'action':'preview','aato_band':'up_to_5cr','ordinary_domestic':'on'})
        for pk in Invoice.objects.filter(user=self.user,invoice_type='sales',status__in=('verified','paid')).values_list('pk',flat=True):
            data[f'registration_{pk}']='unknown';data[f'reason_{pk}']=''
        return data

    def post(self, data):
        return self.client.post(reverse('tax_preview',args=[self.period.pk,'gstr1']),data.urlencode(),content_type='application/x-www-form-urlencoded')

    @override_settings(DATA_UPLOAD_MAX_NUMBER_FIELDS=1000)
    def test_reproduces_old_http_limit(self):
        self.scale(500);self.assertEqual(self.post(self.body()).status_code,400);self.assertEqual(TaxWorkingPaper.objects.count(),0)

    def test_supported_http_capacities(self):
        for count in (1,100,499,500,1000,2000):
            with self.subTest(count=count):
                current=Invoice.objects.filter(user=self.user).count()
                Invoice.objects.bulk_create([Invoice(user=self.user,invoice_type='sales',invoice_number=f'SCALE-{count}-{i}',invoice_date=date(2026,10,1),supplier_name='Synthetic',supplier_gstin=self.business.gstin,status='verified') for i in range(count-current)])
                response=self.post(self.body());self.assertEqual(response.status_code,200);self.assertTrue(response.context['preview_token'],str(response.context['form'].errors));self.assertEqual(len(response.context['data']['included']),count)
        data=self.body();preview=self.post(data);data['action']='prepare';data['confirmed']='on';data['preview_token']=preview.context['preview_token']
        self.assertEqual(self.post(data).status_code,302);self.assertEqual(self.post(data).status_code,302);self.assertEqual(TaxWorkingPaper.objects.count(),1)

    def test_excess_capacity_no_partial_writes(self):
        self.scale(2001);response=self.post(self.body());self.assertContains(response,'at most 2,000');self.assertEqual(TaxWorkingPaper.objects.count(),0)

    def test_malformed_and_foreign_controls(self):
        for key,value in [('registration_999999','unknown'),(f'registration_{self.invoice.pk}','not-a-status')]:
            data=self.body();data[key]=value;response=self.post(data);self.assertEqual(response.status_code,200);self.assertFalse(response.context['preview_token'])
        self.assertEqual(TaxWorkingPaper.objects.count(),0)

    def test_repeated_and_oversized_requests(self):
        data=self.body();data.appendlist('ordinary_domestic','on');self.assertContains(self.post(data),'Repeated controls')
        raw='&'.join(f'x{i}=1' for i in range(15001));self.assertEqual(self.client.post(reverse('tax_preview',args=[self.period.pk,'gstr1']),raw,content_type='application/x-www-form-urlencoded').status_code,400)

    def test_income_forms_omit_registration_controls(self):
        for kind in ('income_tax','income_summary'):
            response=self.client.get(reverse('tax_preview',args=[self.period.pk,kind]));self.assertNotContains(response,f'name="registration_{self.invoice.pk}"')

    def test_csrf_still_required(self):
        client=Client(enforce_csrf_checks=True);client.force_login(self.user)
        self.assertEqual(client.post(reverse('tax_preview',args=[self.period.pk,'gstr1']),{}).status_code,403)


class WorkspacePresentationTests(TestCase):
    setUp = fixtures.PreparationTests.setUp
    create = fixtures.PreparationTests.create
    payload = fixtures.PreparationTests.payload

    def test_own_business_identity_for_two_users(self):
        self.business.business_name='<script>Owner A</script>';self.business.save()
        page=self.client.get(reverse('analytics_dashboard'));self.assertContains(page,'&lt;script&gt;Owner A&lt;/script&gt;');self.assertNotContains(page,'<script>Owner A</script>');self.assertNotContains(page,'SmartKart Traders')
        self.client.force_login(self.other);page=self.client.get(reverse('analytics_dashboard'));self.assertContains(page,self.other_business.business_name);self.assertNotContains(page,'Owner A')

    def test_missing_name_fallback(self):
        self.business.business_name='';self.business.save();self.assertContains(self.client.get(reverse('analytics_dashboard')),'Your business workspace')

    def test_import_provenance(self):
        for name in ('analytics_dashboard','reconciliation_list'):
            self.assertContains(self.client.get(reverse(name)),'not independently authenticated')


class ReviewPanelTests(TestCase):
    setUp = fixtures.PreparationTests.setUp
    create = fixtures.PreparationTests.create
    payload = fixtures.PreparationTests.payload
    inputs = fixtures.PreparationTests.inputs
    paper = fixtures.PreparationTests.paper

    def test_empty_and_no_database_writes(self):
        from audit.models import AuditEvent
        before=AuditEvent.objects.count();response=self.client.get(reverse('tax_period',args=[self.period.pk]));self.assertContains(response,'No records awaiting manual review');self.assertContains(response,'No saved working papers');self.assertEqual(AuditEvent.objects.count(),before)

    def test_counts_links_and_pagination(self):
        for i in range(11):self.create()
        response=self.client.get(reverse('tax_period',args=[self.period.pk]));panel=response.context['panel'];self.assertEqual(panel['review_page'].paginator.count,11);self.assertEqual(len(panel['review_rows']),10);self.assertIn('/draft/',panel['review_rows'][0]['url']);self.assertContains(response,'Next review items')
        self.assertEqual(len(self.client.get(reverse('tax_period',args=[self.period.pk])+'?review_page=2').context['panel']['review_rows']),1)

    def test_pending_itc_link_for_existing_purchase(self):
        inv=fixtures.PreparationTests.purchase(self);page=self.client.get(reverse('tax_period',args=[self.period.pk]));self.assertEqual(page.context['panel']['pending_on_page'],1);self.assertContains(page,reverse('expense_purchase_link'))

    def test_stale_paper_on_demand_without_rewrite(self):
        paper=self.paper();saved=paper.data;Invoice.objects.filter(pk=self.invoice.pk).update(buyer_name='Changed synthetic snapshot')
        page=self.client.get(reverse('tax_period',args=[self.period.pk])+f'?check_paper={paper.pk}');self.assertTrue(page.context['panel']['checked']['changed']);paper.refresh_from_db();self.assertEqual(paper.data,saved)

    def test_foreign_sources_and_papers_denied(self):
        paper=self.paper();self.client.force_login(self.other);response=self.client.get(reverse('tax_preparation'));self.assertNotContains(response,self.invoice.invoice_number);self.assertEqual(self.client.get(reverse('tax_preparation')+f'?check_paper={paper.pk}').status_code,404)


class ExportUsabilityTests(TestCase):
    setUp = fixtures.PreparationTests.setUp
    create = fixtures.PreparationTests.create
    payload = fixtures.PreparationTests.payload
    inputs = fixtures.PreparationTests.inputs
    paper = fixtures.PreparationTests.paper

    def test_readable_and_complete_evidence(self):
        from io import BytesIO
        from openpyxl import load_workbook
        paper=self.paper();response=self.client.get(reverse('tax_paper_export',args=[paper.pk,'csv']));self.assertIn(b'Tax period start,2026-10-01',response.content);self.assertIn(b'snapshot.source_evidence.invoices[0]',response.content);self.assertIn(b'224.20',response.content)
        wb=load_workbook(BytesIO(self.client.get(reverse('tax_paper_export',args=[paper.pk,'xlsx'])).content));self.assertIn('Readable summary',wb.sheetnames);self.assertTrue(any(cell.value=='224.20' for row in wb['Readable summary'] for cell in row))

    def test_large_pdf_returns_frozen_export_alternatives(self):
        from unittest.mock import patch
        from invoices.pdf_generator import PDFGenerationError
        paper=self.paper()
        with patch('invoices.pdf_generator.render_report_pdf',side_effect=PDFGenerationError('limit')):
            response=self.client.get(reverse('tax_paper_export',args=[paper.pk,'pdf']));self.assertEqual(response.status_code,400);self.assertContains(response,'Complete evidence as CSV',status_code=400)

    def test_negative_saved_values_and_formula_guard(self):
        from .preparation_exports import export_paper
        from copy import deepcopy
        paper=self.paper();data=deepcopy(paper.data);data['gstr1']['buckets']['B2CS']['total']='-1180.03';paper.data=data
        response=export_paper(paper,'csv');self.assertIn(b'-1180.03',response.content);self.assertIn(b'Complete saved evidence',response.content)

    def test_xml_control_characters_fail_safely(self):
        from .preparation_exports import export_paper
        from django.core.exceptions import ValidationError
        paper=self.paper();paper.data['included'][0]['buyer_name']='Bad\x01value'
        with self.assertRaises(ValidationError):export_paper(paper,'xlsx')

    def test_inclusive_and_mixed_saved_export_parity(self):
        from invoices import test_manual
        import csv,io
        for updates in [dict(pricing_mode='inclusive',**{'items-0-quantity':'1','items-0-rate':'1180.03','items-0-discount':'0'}),dict(**{'items-TOTAL_FORMS':'2','items-0-quantity':'1','items-0-rate':'100','items-0-discount':'0','items-1-description':'Second service','items-1-hsn_sac_code':'9983','items-1-quantity':'1','items-1-unit':'Hour','items-1-rate':'100','items-1-discount':'0','items-1-tax_rate':'5'})]:
            invoice=self.create(**updates);test_manual.ManualInvoiceTests.finalize(self,invoice)
        paper=self.paper();response=self.client.get(reverse('tax_paper_export',args=[paper.pk,'csv']));rows=dict(list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))[1:])
        for index,row in enumerate(paper.data['included']):
            for key,value in row['values'].items():self.assertEqual(rows[f'snapshot.included[{index}].values.{key}'],value)
        self.assertContains(self.client.get(reverse('tax_paper',args=[paper.pk])),'1180.03')
        self.assertEqual(self.client.get(reverse('tax_paper_export',args=[paper.pk,'pdf'])).status_code,200)

    def test_actual_credit_note_negative_period_exports(self):
        from invoices import test_adjustments
        from .preparation import create_period
        note=test_adjustments.AdjustmentHelpers.create_note(self,document_date=date(2026,10,15),rows=[{'original_line':self.invoice.line_items.first(),'mode':'value','quantity':0,'amount':Decimal('50')}])
        test_adjustments.AdjustmentHelpers.finalize_note(self,note)
        self.period=create_period(self.request,'custom',date(2026,10,15),date(2026,10,15));paper=self.paper(inputs=self.inputs(registration={}))
        self.assertEqual(paper.data['gstr1']['net_gst_adjusted_outward']['total'],'-59.00')
        for fmt in ('csv','xlsx','pdf'):
            response=self.client.get(reverse('tax_paper_export',args=[paper.pk,fmt]));self.assertEqual(response.status_code,200)
        self.assertIn(b'-59.00',self.client.get(reverse('tax_paper_export',args=[paper.pk,'csv'])).content)

    def test_all_formats_reject_another_owner(self):
        paper=self.paper();self.client.force_login(self.other)
        for fmt in ('csv','xlsx','pdf'):self.assertEqual(self.client.get(reverse('tax_paper_export',args=[paper.pk,fmt])).status_code,404)


class AdditionalSafetyTests(TestCase):
    setUp = fixtures.PreparationTests.setUp
    create = fixtures.PreparationTests.create
    payload = fixtures.PreparationTests.payload
    inputs = fixtures.PreparationTests.inputs
    paper = fixtures.PreparationTests.paper

    def test_review_panel_checks_only_visible_inputs(self):
        from unittest.mock import patch
        Invoice.objects.bulk_create([Invoice(user=self.user,invoice_type='expense',invoice_number=f'INPUT-{i}',invoice_date=date(2026,10,1),status='verified') for i in range(15)])
        with patch('taxation.readiness.itc_state',return_value={'status':'pending','reason':'Synthetic pending','amount':0}) as state:
            response=self.client.get(reverse('tax_period',args=[self.period.pk]));self.assertEqual(response.context['panel']['input_page'].paginator.count,15);self.assertEqual(state.call_count,10);self.assertEqual(response.context['panel']['pending_on_page'],10)

    def test_dashboard_authorization_redirects(self):
        self.client.logout()
        for name in ('analytics_dashboard','audit_log','tax_preparation'):
            response=self.client.get(reverse(name));self.assertEqual(response.status_code,302);self.assertIn('/accounts/login/',response.url)

    def test_full_export_parity_and_frozen_body(self):
        import csv,io
        from copy import deepcopy
        from openpyxl import load_workbook
        import pdfplumber
        paper=self.paper();saved=deepcopy(paper.data)
        csv_response=self.client.get(reverse('tax_paper_export',args=[paper.pk,'csv']));csv_rows=dict(list(csv.reader(io.StringIO(csv_response.content.decode('utf-8-sig'))))[1:])
        excel=self.client.get(reverse('tax_paper_export',args=[paper.pk,'xlsx']));wb=load_workbook(io.BytesIO(excel.content));xlsx_rows=dict(wb['Saved working paper'].values)
        with pdfplumber.open(io.BytesIO(self.client.get(reverse('tax_paper_export',args=[paper.pk,'pdf'])).content)) as pdf:
            text=' '.join(page.extract_text() or '' for page in pdf.pages);self.assertTrue(all(abs(page.width-595.28)<.1 and abs(page.height-841.89)<.1 for page in pdf.pages))
        for component,value in paper.data['included'][0]['values'].items():
            path=f'snapshot.included[0].values.{component}';self.assertEqual(csv_rows[path],value);self.assertEqual(xlsx_rows[path],value);self.assertIn(value,text)
        self.assertEqual(self.client.get(reverse('tax_paper',args=[paper.pk])).context['data'],saved);paper.refresh_from_db();self.assertEqual(paper.data,saved)

    def test_real_large_report_retains_complete_csv_and_excel(self):
        import csv,io
        from openpyxl import load_workbook
        Invoice.objects.bulk_create([Invoice(user=self.user,invoice_type='sales',invoice_number=f'EXPORT-{i}',invoice_date=date(2026,10,1),supplier_name='Synthetic',supplier_gstin=self.business.gstin,status='verified') for i in range(100)])
        paper=self.paper();self.assertEqual(len(paper.data['included']),101)
        for fmt in ('csv','xlsx'):
            response=self.client.get(reverse('tax_paper_export',args=[paper.pk,fmt]));self.assertEqual(response.status_code,200)
            if fmt=='csv':rows=dict(list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))[1:])
            else:rows=dict(load_workbook(io.BytesIO(response.content))['Saved working paper'].values)
            self.assertIn('snapshot.included[100].id',rows)
        self.assertContains(self.client.get(reverse('tax_paper_export',args=[paper.pk,'pdf'])),'Complete evidence as CSV',status_code=400)

    def test_optional_due_date_is_preserved_without_invention(self):
        from .preparation_exports import export_paper
        Invoice.objects.filter(pk=self.invoice.pk).update(due_date=None)
        paper=self.paper();self.assertIsNone(paper.data['included'][0]['due_date']);self.assertIn(b'snapshot.included[0].due_date,',export_paper(paper,'csv').content)
