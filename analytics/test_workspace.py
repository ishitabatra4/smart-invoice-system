"""Presentation regressions with synthetic fixtures; existing money services stay authoritative."""
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import connection
from django.template import engines
from django.test import TestCase, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse, resolve
from django.utils import timezone
from accounts.models import BusinessProfile
from expenses.models import ExpenseCategory, ExpenseDetail
from invoices.models import Invoice
from invoices.views import invoice_list_view
from invoices.templatetags.workspace_ui import pagination_query, workspace_navigation
from analytics.services import build_analytics_context, dashboard_date_range


class WorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user('ui-owner', email='ui@example.test', password='synthetic-password')
        cls.other = get_user_model().objects.create_user('ui-other', email='foreign@example.test', password='synthetic-password')
        cls.business = BusinessProfile.objects.create(user=cls.user, business_name='Synthetic UI Business')
        BusinessProfile.objects.create(user=cls.other, business_name='Foreign UI Business')
        cls.category = ExpenseCategory.objects.create(business=cls.business, name='Synthetic costs')
        for index in range(32):
            Invoice.objects.create(user=cls.user, invoice_type='sales', invoice_number=f'UI-{index:03}', invoice_date=date(2026,11,1), buyer_name='Synthetic buyer', taxable_amount='1000', cgst_amount='90', sgst_amount='90',  total_amount='1180',status='verified')
        Invoice.objects.create(user=cls.other, invoice_type='sales', invoice_number='FOREIGN-NOT-VISIBLE', invoice_date=date(2026,11,1),total_amount='999999',status='verified')

    def setUp(self):self.client.force_login(self.user)
    def request(self,path='/invoices/'):
        request=RequestFactory().get(path);request.user=self.user;request.resolver_match=resolve(request.path);return request
    def expense(self,index,status='verified'):
        invoice=Invoice.objects.create(user=self.user,invoice_type='expense',invoice_number=f'EXP-UI-{index}',invoice_date=date(2026,11,1),taxable_amount='100',total_amount='100',status=status)
        ExpenseDetail.objects.create(invoice=invoice,category=self.category,title='Synthetic expense')
        return invoice

    def test_invoice_list_is_owned_and_paginated(self):
        response=self.client.get(reverse('invoice_list'));self.assertEqual(len(response.context['invoices']),25);self.assertEqual(response.context['page'].paginator.count,32);self.assertNotContains(response,'FOREIGN-NOT-VISIBLE')
        second=self.client.get(reverse('invoice_list'),{'page':2});self.assertEqual(len(second.context['invoices']),7)
        self.assertFalse(set(i.pk for i in response.context['invoices']) & set(i.pk for i in second.context['invoices']))
    def test_invoice_search_type_status_and_sort(self):
        purchase=Invoice.objects.create(user=self.user,invoice_type='purchase',invoice_number='SPECIAL-PURCHASE',invoice_date=date(2026,11,1),status='review')
        response=self.client.get(reverse('invoice_list'),{'type':'purchase','status':'review','q':'SPECIAL','sort':'number'});self.assertEqual(list(response.context['invoices']),[purchase])
        self.assertContains(response,'value="purchase" selected');self.assertContains(response,'value="review" selected')
    def test_invalid_filters_do_not_broaden(self):
        for params in [{'type':'bogus'},{'status':'bogus'},{'sort':'raw_sql'},{'q':'a'*101}]:
            response=self.client.get(reverse('invoice_list'),params);self.assertEqual(response.status_code,200);self.assertFalse(response.context['invoices']);self.assertTrue(response.context['filter_form'].errors)
    def test_invalid_pages_are_handled(self):
        for page in ['bad','-1','99999999999999999999999']:
            self.assertEqual(self.client.get(reverse('invoice_list'),{'page':page}).status_code,200)
    def test_pagination_preserves_filters_without_repeated_page(self):
        req=self.request('/invoices/?q=UI&type=sales&page=2&page=3');query=pagination_query({'request':req});self.assertNotIn('page=',query);self.assertIn('type=sales',query)
    def test_navigation_has_one_active_link_and_real_destinations(self):
        for path in ['/analytics/','/invoices/?type=purchase','/invoices/commercial/','/expenses/list/','/tax/prepare/','/audit/monitor/']:
            req=self.request(path);nav=workspace_navigation({'request':req});links=[link for group in nav['groups'] for link in group['links']];urls=[link['url'] for link in links]
            self.assertEqual(sum(link['active'] for link in links),0 if path=='/invoices/commercial/' else 1)
            for url in urls:resolve(url.split('?')[0].split('#')[0])
    def test_purchase_navigation_opens_correct_group(self):
        result=workspace_navigation({'request':self.request('/invoices/?type=purchase')});self.assertTrue(next(g for g in result['groups'] if g['key']=='invoices')['active'])
    def test_shell_has_accessible_landmarks_and_real_context(self):
        response=self.client.get(reverse('invoice_list'));self.assertContains(response,'Skip to content');self.assertContains(response,'data-workspace-menu');self.assertContains(response,'aria-current="page"');self.assertContains(response,'Synthetic UI Business');self.assertNotContains(response,'Personal workspace')
    def test_shared_feedback_only_once(self):
        req=self.request();req.session={};req._messages=FallbackStorage(req);messages.success(req,'Unique feedback message')
        self.assertContains(invoice_list_view(req),'Unique feedback message',count=1)
    def test_dashboard_totals_exactly_reuse_authoritative_context(self):
        start,end,preset,error=dashboard_date_range({},timezone.localdate());expected=build_analytics_context(self.user,start,end,preset,error)
        response=self.client.get(reverse('analytics_dashboard'))
        for key in ['totals','gross_original','adjustment_events','net_cash_flow','chart_data','treatment_summary']:self.assertEqual(response.context[key],expected[key])
        self.assertNotContains(response,'FOREIGN-NOT-VISIBLE');self.assertLessEqual(len(response.context['recent_invoices']),6)
    def test_dashboard_has_all_workflows_and_charts_without_duplicate_primary_metrics(self):
        response=self.client.get(reverse('analytics_dashboard'));self.assertContains(response,'Create invoice');self.assertContains(response,'Upload receipt');self.assertContains(response,'Prepare tax report')
        self.assertContains(response,'Cash, purchases &amp; party insights');self.assertContains(response,'GST &amp; treatment details');self.assertContains(response,'id="gstBreakdownChart"');self.assertContains(response,'id="invoiceStatusChart"');self.assertContains(response,'Accounts Receivable',count=1)
    def test_no_foreign_alerts_on_business_dashboard(self):
        from audit.models import SecurityAlert
        SecurityAlert.objects.create(business=BusinessProfile.objects.get(user=self.other),fingerprint='a'*64,rule_id='AUTH_FAILURE_BURST',severity='HIGH',summary='FOREIGN-ALERT')
        self.assertNotContains(self.client.get(reverse('analytics_dashboard')),'FOREIGN-ALERT')
    def test_recent_lists_are_activity_not_date_filtered_financial_totals(self):
        self.expense(1);response=self.client.get(reverse('analytics_dashboard'),{'period':'custom','start_date':'2025-01-01','end_date':'2025-01-31'});self.assertEqual(response.context['totals']['sales'],Decimal('0.00'));self.assertEqual(len(response.context['recent_expenses']),1);self.assertContains(response,'across all dates')
    def test_expense_default_listing_is_page_bounded(self):
        for index in range(28):self.expense(index)
        response=self.client.get(reverse('expense_list'));self.assertEqual(len(response.context['rows']),25);self.assertEqual(response.context['page'].paginator.count,28)
        second=self.client.get(reverse('expense_list'),{'page':2});self.assertEqual(len(second.context['rows']),3)
    def test_expense_derived_filters_preserve_meaning_before_pagination(self):
        for index in range(28):self.expense(index)
        response=self.client.get(reverse('expense_list'),{'itc':'pending','status':'unpaid','page':2});self.assertEqual(len(response.context['rows']),3);self.assertEqual(response.context['page'].paginator.count,28)
        self.assertTrue(all(row['payment_state']=='unpaid' and row['itc']['status']=='pending' for row in response.context['rows']));self.assertContains(response,'value="pending" selected')
    def test_adjustment_filter_state_and_pagination_query(self):
        response=self.client.get(reverse('adjustment_list'),{'kind':'credit','status':'cancelled','sort':'number','page':2});self.assertContains(response,'value="credit" selected');self.assertContains(response,'value="cancelled" selected');self.assertContains(response,'value="number" selected')
    def test_shared_styles_and_js_on_major_modules(self):
        for name in ['invoice_list','expense_list','commercial_list','adjustment_list','tax_preparation','reconciliation_list','audit_events','security_monitor','profile']:
            response=self.client.get(reverse(name));self.assertEqual(response.status_code,200);self.assertContains(response,'css/tokens.css');self.assertContains(response,'js/workspace.js')
    def test_all_templates_compile(self):
        for path in Path('templates').rglob('*.html'):engines['django'].get_template(str(path.relative_to('templates')))
    def test_get_listing_does_not_change_financial_records(self):
        before=list(Invoice.objects.values());self.client.get(reverse('invoice_list'));self.client.get(reverse('analytics_dashboard'));self.assertEqual(list(Invoice.objects.values()),before)
    def test_invoice_query_page_is_bounded_without_relation_queries(self):
        req=self.request()
        with CaptureQueriesContext(connection) as queries:response=invoice_list_view(req)
        self.assertEqual(response.status_code,200);self.assertLess(len(queries),8);self.assertTrue(any('LIMIT 25' in q['sql'] for q in queries))

    def test_navigation_accepts_shared_commercial_email_context(self):
        from types import SimpleNamespace
        result = workspace_navigation({'request': self.request('/invoices/commercial/1/email/'), 'invoice': SimpleNamespace(kind='quotation')})
        self.assertTrue(next(g for g in result['groups'] if g['key']=='quotations')['active'])
    def test_missing_business_name_has_explicit_workspace_fallback(self):
        self.business.business_name='';self.business.save()
        self.assertContains(self.client.get(reverse('analytics_dashboard')),'Your business workspace')

    def test_edited_date_flag_and_submitted_preset_agree(self):
        response=self.client.get(reverse('analytics_dashboard'),{'period':'this_month','dates_edited':'1','start_date':'2026-10-01','end_date':'2026-10-31'})
        self.assertEqual(response.context['selected_period'],'custom');self.assertEqual(response.context['start_date'],date(2026,10,1));self.assertEqual(response.context['filter_end_date'],'2026-10-31')
    def test_preset_renders_active_dates_instead_of_stale_inputs(self):
        response=self.client.get(reverse('analytics_dashboard'),{'period':'last_month','dates_edited':'0','start_date':'2020-01-01','end_date':'2020-01-02'})
        self.assertEqual(response.context['filter_start_date'],response.context['start_date'].isoformat());self.assertNotEqual(response.context['filter_start_date'],'2020-01-01')
    def test_invalid_manual_dates_have_visible_feedback(self):
        for start,end in [('0001-01-01','0001-01-02'),('2026-11-02','2026-11-01'),('2020-01-01','2035-01-01')]:
            response=self.client.get(reverse('analytics_dashboard'),{'period':'custom','start_date':start,'end_date':end})
            self.assertEqual(response.status_code,200);self.assertIsNotNone(response.context['custom_error']);self.assertEqual(response.context['selected_period'],'this_month')
