"""Presentation-only navigation and query links; no database access."""
from django import template
from django.urls import reverse
from urllib.parse import parse_qsl

register = template.Library()


@register.inclusion_tag('components/workspace_navigation.html', takes_context=True)
def workspace_navigation(context):
    request=context['request']
    match=request.resolver_match
    name=match.url_name if match else ''
    kwargs=match.kwargs if match else {}
    invoice=context.get('invoice')
    document=context.get('document') or context.get('note') or invoice
    kind=getattr(invoice,'invoice_type',request.GET.get('type',kwargs.get('invoice_type','')))
    role=request.GET.get('role','')
    party=context.get('party')
    if party is not None:role='supplier' if party.party_type=='vendor' else 'customer'
    # Each destination is reversed; queries below are display filters, never actions.
    def link(label,route,query='',arguments=None,hint=''):
        return {'label':label,'route':route,'url':reverse(route,kwargs=arguments)+query,'query':query,'active':False,'hint':hint,'arguments':arguments or {}}
    def menu(key,label,links):return {'key':key,'label':label,'links':links,'active':False}
    tax_hint='Select a period, then choose this working-paper type; no return is filed.'
    sections=[
        ('overview','Overview',[menu('overview','',[link('Dashboard','analytics_dashboard')])]),
        ('invoices','Invoice Management',[
            menu('invoices','Invoices',[link('All Invoices','invoice_list'),link('Sales Invoices','invoice_list','?type=sales'),link('Purchase Invoices','invoice_list','?type=purchase'),link('Create Invoice','invoice_create',arguments={'invoice_type':'sales'},hint='Create a sales draft; purchase creation remains available from the invoice pages.')]),
            menu('extraction','Upload & Extraction',[link('Upload Invoice','invoice_upload'),link('Review Queue','invoice_list','?status=review'),link('Extraction History','extraction_history')])]),
        ('parties','Party Management',[
            menu('customers','Customers',[link('All Customers','party_list','?role=customer'),link('Add Customer','party_add','?role=customer')]),
            menu('suppliers','Suppliers',[link('All Suppliers','party_list','?role=supplier'),link('Add Supplier','party_add','?role=supplier')])]),
        ('commercial','Commercial Documents',[
            menu('quotations','Quotations',[link('All Quotations','commercial_list','?kind=quotation'),link('Create Quotation','commercial_create',arguments={'kind':'quotation'})]),
            menu('proformas','Proforma Invoices',[link('All Proformas','commercial_list','?kind=proforma'),link('Create Proforma','commercial_create',arguments={'kind':'proforma'})])]),
        ('adjustments','Adjustments',[
            menu('credit','Credit Notes',[link('Issued Credit Notes','adjustment_list','?kind=credit&direction=issued'),link('Received Credit Notes','adjustment_list','?kind=credit&direction=received')]),
            menu('debit','Debit Notes',[link('Issued Debit Notes','adjustment_list','?kind=debit&direction=issued'),link('Received Debit Notes','adjustment_list','?kind=debit&direction=received')])]),
        ('finance','Finance',[
            menu('payments','Payments',[link('All Payments','party_list',hint='Open a party ledger for its existing payment history; there is no standalone all-payments register.')]),
            menu('expenses','Expenses',[link('All Expenses','expense_list'),link('Add Expense','expense_add')]),
            menu('recurring','Recurring Expenses',[link('All Schedules','recurring_expense_list'),link('Create Schedule','recurring_expense_add')])]),
        ('tax','Taxation',[
            menu('reconciliation','GST Reconciliation',[link('Upload GSTR-2B','reconciliation_list','#return_period',hint='Upload form on the reconciliation page; the upload endpoint accepts POST only.'),link('Reconciliation History','reconciliation_list')]),
            menu('itc','',[link('ITC Review','expense_reports','?report_type=itc')]),
            menu('preparation','Tax Preparation',[link(title,'tax_preparation',hint=tax_hint) for title in ['GSTR-1','GSTR-3B','ITC Summary','GST Liability','Income Summary','Income Tax']])]),
        ('reports','Reporting',[menu('reports','Financial Reports',[
            link('Sales Report','adjustment_report',hint='Existing combined sales/purchase and adjustment report.'),link('Purchase Report','adjustment_report',hint='Existing combined sales/purchase and adjustment report.'),link('Expense Report','expense_reports'),link('Receivables','outstanding_dashboard','?party_type=customer'),link('Payables','outstanding_dashboard','?party_type=supplier'),link('Cash Flow','analytics_dashboard','#cashFlowChart')])]),
        ('admin','Administration',[menu('admin','',[link('Activity Logs','audit_events'),link('Security Monitoring','security_monitor'),link('Business Settings','profile')])]),
    ]
    groups=[group for _,_,menus in sections for group in menus]
    target=name;preferred=None
    if name=='dashboard':target='analytics_dashboard'
    elif name=='invoice_create' and kind=='purchase':target='invoice_list'
    elif name.startswith('commercial_') and name not in ('commercial_list','commercial_create'):
        target='commercial_list';preferred='quotations' if getattr(document,'kind','quotation')=='quotation' else 'proformas'
    elif name.startswith('adjustment_') and name not in ('adjustment_report','adjustment_list'):
        target='adjustment_list';preferred=getattr(document,'kind','credit')
    elif name.startswith('party_') and name not in ('party_list','party_add'):target='party_list';preferred='suppliers' if role=='supplier' else 'customers'
    elif name.startswith('recurring_') and name not in ('recurring_expense_list','recurring_expense_add'):target='recurring_expense_list'
    elif name.startswith('reconciliation_') and name!='reconciliation_upload':target='reconciliation_list'
    elif name.startswith('tax_'):target='tax_preparation'
    elif name.startswith('audit_'):target='audit_events'
    elif name.startswith('security_'):target='security_monitor'
    elif name in ('profile','password_change'):target='profile'
    elif name=='expense_report_export':target='expense_reports'
    elif name.startswith('expense_') and name not in ('expense_add','expense_reports'):target='expense_list'
    elif name.startswith('extraction_') and name!='extraction_history':target='extraction_history'
    elif name.startswith('invoice_') and name not in ('invoice_list','invoice_create','invoice_upload'):target='invoice_list'
    candidates=[]
    for group in groups:
        for entry in group['links']:
            if entry['route']!=target:continue
            if name==target and any(kwargs.get(key)!=value for key,value in entry['arguments'].items()):continue
            params=dict(parse_qsl(entry['query'].lstrip('?'))) if entry['query'].startswith('?') else {}
            actual=dict(request.GET.items())
            if kind:actual['type']=kind
            if role:actual['role']=role
            if preferred in ('quotations','proformas'):actual['kind']='quotation' if preferred=='quotations' else 'proforma'
            if preferred in ('credit','debit'):actual.update(kind=preferred,direction=getattr(document,'direction','issued'))
            if any(actual.get(key)!=value for key,value in params.items()):continue
            score=len(params)*10+(5 if params.get('status')=='review' else 0)+(100 if preferred==group['key'] else 0)
            if entry['query'].startswith('#'):continue
            if target=='tax_preparation':
                titles={'gstr1':'GSTR-1','gstr3b':'GSTR-3B','itc':'ITC Summary','liability':'GST Liability','income_summary':'Income Summary','income_tax':'Income Tax'}
                if entry['label']==titles.get(context.get('kind',kwargs.get('kind'))):score+=10
            candidates.append((score,group,entry))
    if candidates:
        _,group,entry=max(candidates,key=lambda row:row[0]);entry['active']=True;group['active']=True
    elif target in ('commercial_list','adjustment_list'):
        # A combined unfiltered listing belongs to both menus, not either filtered link.
        keys=('quotations','proformas') if target=='commercial_list' else ('credit','debit')
        for group in groups:
            if group['key'] in keys:group['active']=True
    return {'sections':[{'key':key,'label':label,'menus':menus} for key,label,menus in sections],'groups':groups,'overview_active':name in ('dashboard','analytics_dashboard')}


@register.simple_tag(takes_context=True)
def pagination_query(context):
    params = context['request'].GET.copy()
    params.pop('page', None)
    return params.urlencode()
