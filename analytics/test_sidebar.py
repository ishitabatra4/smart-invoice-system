"""Navigation-only checks; all mutations confined to Django's test database."""
from pathlib import Path
from html.parser import HTMLParser
from django.test import TestCase,RequestFactory
from django.contrib.auth import get_user_model
from django.urls import resolve,reverse
from accounts.models import BusinessProfile,BusinessParty
from invoices.templatetags.workspace_ui import workspace_navigation

class SidebarTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner=get_user_model().objects.create_user('sidebar-owner',email='sidebar@example.test',password='test-only')
        cls.other=get_user_model().objects.create_user('sidebar-other',email='other@example.test',password='test-only')
        cls.business=BusinessProfile.objects.create(user=cls.owner,business_name='Sidebar Fictional Business')
        cls.foreign=BusinessProfile.objects.create(user=cls.other,business_name='Foreign Fictional Business')
        for role in ('customer','vendor','both'):
            BusinessParty.objects.create(business=cls.business,name='Local '+role,party_type=role)
        BusinessParty.objects.create(business=cls.foreign,name='PRIVATE FOREIGN',party_type='both')

    def setUp(self):self.client.force_login(self.owner)
    def nav(self,path,**context):
        req=RequestFactory().get(path);req.user=self.owner;req.resolver_match=resolve(req.path)
        return workspace_navigation({'request':req,**context})
    def selected(self,path,**context):
        nav=self.nav(path,**context);links=[x for g in nav['groups'] for x in g['links'] if x['active']]
        self.assertEqual(len(links),1,path)
        return links[0]['label']

    def test_hierarchy_and_all_destinations_resolve_and_render(self):
        nav=self.nav('/invoices/');self.assertEqual(len(nav['sections']),9)
        links=[x for g in nav['groups'] for x in g['links']]
        self.assertGreater(len(links),40)
        for url in sorted({x['url'] for x in links}):
            with self.subTest(url=url):
                resolve(url.split('?')[0].split('#')[0]);response=self.client.get(url,follow=True)
                self.assertEqual(response.status_code,200)
        self.assertNotContains(self.client.get('/invoices/'),'PRIVATE FOREIGN')

    def test_exact_filter_active_links(self):
        for path,label in [('/invoices/','All Invoices'),('/invoices/?type=sales','Sales Invoices'),('/invoices/?type=purchase','Purchase Invoices'),('/invoices/?status=review','Review Queue'),('/invoices/upload/','Upload Invoice'),('/invoices/extractions/','Extraction History'),('/invoices/commercial/?kind=proforma','All Proformas'),('/invoices/commercial/create/proforma/','Create Proforma'),('/invoices/commercial/create/quotation/','Create Quotation'),('/invoices/adjustments/?kind=credit&direction=received','Received Credit Notes'),('/invoices/adjustments/?kind=debit&direction=issued','Issued Debit Notes'),('/expenses/reports/?report_type=itc','ITC Review'),('/expenses/reports/','Expense Report'),('/invoices/outstanding/?party_type=supplier','Payables'),('/invoices/parties/?role=customer','All Customers'),('/invoices/parties/?role=supplier','All Suppliers'),('/invoices/parties/add/?role=supplier','Add Supplier')]:
            with self.subTest(path=path):self.assertEqual(self.selected(path),label)

    def test_active_menu_is_expanded_and_link_announced(self):
        response=self.client.get('/invoices/?type=purchase')
        self.assertContains(response,'data-nav-accordion open')
        self.assertContains(response,'aria-current="page"',count=1)
        self.assertContains(response,'Invoice Management');self.assertContains(response,'Log out')
        self.assertContains(response,'Sidebar Fictional Business')

    def test_role_filters_include_both_without_duplication_or_cross_business(self):
        count=BusinessParty.objects.count()
        for role,expected in [('customer',{'Local customer','Local both'}),('supplier',{'Local vendor','Local both'})]:
            response=self.client.get('/invoices/parties/',{'role':role})
            self.assertEqual({r['party'].name for r in response.context['party_rows']},expected)
            self.assertEqual(len(response.context['party_rows']),2)
        self.assertEqual(BusinessParty.objects.count(),count)
        self.assertEqual(len(self.client.get('/invoices/parties/').context['party_rows']),3)

    def test_add_party_preselection_does_not_write(self):
        before=BusinessParty.objects.count()
        for role,expected in [('customer','customer'),('supplier','vendor')]:
            response=self.client.get('/invoices/parties/add/',{'role':role})
            self.assertEqual(response.context['form']['party_type'].value(),expected)
        self.assertEqual(BusinessParty.objects.count(),before)

    def test_unauthenticated_destinations_and_security_mutations_stay_protected(self):
        self.client.logout()
        for url in ['/invoices/','/invoices/parties/?role=customer','/audit/monitor/','/tax/prepare/']:
            self.assertEqual(self.client.get(url).status_code,302)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('security_rules')).status_code,403)

    def test_tax_period_parent_mapping_and_kind_selection(self):
        self.assertEqual(self.selected('/tax/prepare/'),'GSTR-1')
        self.assertEqual(self.selected('/tax/prepare/period/1/income_tax/',kind='income_tax'),'Income Tax')

    def test_responsive_styles_have_scoped_scroll_motion_and_mobile_bounds(self):
        css=Path('static/css/style.css').read_text()
        for rule in ('min-height:0; flex:1; overflow-y:auto','max-height:70dvh','max-width:100%','prefers-reduced-motion:reduce','min-height:42px'):
            self.assertIn(rule,css)

    def test_populated_role_filter_preserves_full_both_party_balances(self):
        from invoices.models import Invoice
        from datetime import date
        from decimal import Decimal
        both=BusinessParty.objects.get(business=self.business,party_type='both')
        vendor=BusinessParty.objects.get(business=self.business,party_type='vendor')
        for party,kind,amount in [(vendor,'purchase',50),(both,'sales',100),(both,'purchase',80)]:
            Invoice.objects.create(user=self.owner,invoice_type=kind,invoice_date=date(2026,11,1),status='verified',total_amount=amount,taxable_amount=amount,**({'buyer_party':party} if kind=='sales' else {'supplier_party':party}))
        for role in ('customer','supplier'):
            response=self.client.get('/invoices/parties/',{'role':role})
            self.assertEqual(response.status_code,200)
            row=next(r for r in response.context['party_rows'] if r['party']==both)
            self.assertEqual(row['receivable'],Decimal(100));self.assertEqual(row['payable'],Decimal(80))
