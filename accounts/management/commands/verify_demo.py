"""Exercise the populated demo using real Django routes, without live delivery."""
import json
import secrets
from datetime import date
from decimal import Decimal as D
from io import BytesIO
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import BaseCommand, CommandError
from django.db import connection
from django.test import Client
from django.urls import reverse
from config.demo_safety import require_demo


class Command(BaseCommand):
    help = 'Verify synthetic demo routes, exports, isolation and session revocation. Writes demo audit/session records only.'
    requires_system_checks = []

    def handle(self,*args,**options):
        try: root=require_demo()
        except ImproperlyConfigured as exc: raise CommandError(str(exc)) from None
        if not (root/'inventory.json').exists():raise CommandError('Run setup_demo first.')
        from accounts.demo_data import verify_inventory, counts
        from invoices.models import Invoice, InvoicePayment, InvoiceAdjustmentNote, CommercialDocument
        from expenses.models import ExpenseDetail
        from taxation.models import TaxWorkingPaper, ReconciliationRecord
        from audit.models import SecurityAlert, SessionSecurityRecord
        from audit.security import session_digest
        from analytics.services import build_analytics_context
        from invoices.ledger import invoice_outstanding
        inventory=json.loads((root/'inventory.json').read_text());verify_inventory(inventory)
        def financial_snapshot():
            return json.dumps({m._meta.label:list(m.objects.order_by('pk').values()) for m in
                (Invoice,InvoicePayment,InvoiceAdjustmentNote,CommercialDocument,ExpenseDetail,TaxWorkingPaper,ReconciliationRecord)},default=str,sort_keys=True)
        before=financial_snapshot();checks=[];exports=[]
        owners=list(get_user_model().objects.filter(username__in=['lumen-owner','cobalt-owner']).order_by('pk'))
        def require(condition,message):
            if not condition:raise CommandError(message)
        def content(response):
            data=b''.join(response.streaming_content) if response.streaming else response.content
            response.close();return data
        def get(client,name,args=(),params=None,expected=200):
            response=client.get(reverse(name,args=args),params or {})
            require(response.status_code==expected,f'{name} returned {response.status_code}, expected {expected}.')
            checks.append({'route':name,'status':response.status_code})
            return response
        for user in owners:
            client=Client();client.force_login(user)
            for name in ('analytics_dashboard','invoice_list','party_list','outstanding_dashboard','commercial_list','adjustment_list','expense_dashboard','expense_list','recurring_expense_list','expense_reports','extraction_history','tax_preparation','reconciliation_list','audit_log','security_monitor','security_sessions'):
                content(get(client,name))
            owned=Invoice.objects.filter(user=user,entry_mode='manual',document_state='finalized').first()
            foreign=Invoice.objects.filter(user=owners[1] if user==owners[0] else owners[0],entry_mode='manual').first()
            for name in ('invoice_detail','invoice_preview','invoice_pdf'):
                content(get(client,name,[owned.pk]))
                content(get(client,name,[foreign.pk],expected=404))
            foreign_party=foreign.buyer_party_id or foreign.supplier_party_id
            content(get(client,'party_detail',[foreign_party],expected=404))
            foreign_quote=CommercialDocument.objects.exclude(user=user).first()
            for name in ('commercial_detail','commercial_pdf'):
                content(get(client,name,[foreign_quote.pk],expected=404))
            for note in InvoiceAdjustmentNote.objects.filter(user=user):
                content(get(client,'adjustment_detail',[note.pk]))
            for doc in CommercialDocument.objects.filter(user=user):
                content(get(client,'commercial_detail',[doc.pk]))
            # Export bytes exercise existing renderers, not a substitute for browser/printing acceptance.
            inv=owned
            export_specs=[('invoice_pdf',[inv.pk],None),('party_ledger_export',[inv.buyer_party_id or inv.supplier_party_id],None)]
            if user==owners[0]:
                note=InvoiceAdjustmentNote.objects.filter(user=user).first();doc=CommercialDocument.objects.filter(user=user).first()
                export_specs += [('adjustment_pdf',[note.pk],None),('commercial_pdf',[doc.pk],None)]
                for fmt in ('csv','xlsx','pdf'):
                    export_specs += [('expense_report_export',[fmt],{'period':'custom','start_date':'2026-10-01','end_date':'2026-11-30'}),('tax_paper_export',[2,fmt],None)]
            for name,args,params in export_specs:
                response=get(client,name,args,params);mime=response.get('Content-Type','');data=content(response)
                require(len(data)>50,'Empty demo export.')
                detail={'route':name,'format':mime,'bytes':len(data)}
                if 'pdf' in mime:
                    import pdfplumber
                    with pdfplumber.open(BytesIO(data)) as pdf:
                        require(bool(pdf.pages),'PDF has no pages.')
                        if name in ('invoice_pdf','commercial_pdf','adjustment_pdf'):
                            require(all(abs(min(p.width,p.height)-595.28)<2 and abs(max(p.width,p.height)-841.89)<2 for p in pdf.pages),'Invoice/document PDF is not A4.')
                        detail['page_sizes']=[{'width':float(p.width),'height':float(p.height)} for p in pdf.pages]
                        detail['pages']=len(pdf.pages)
                elif 'spreadsheet' in mime:
                    from openpyxl import load_workbook
                    workbook=load_workbook(BytesIO(data),read_only=True);require(bool(workbook.sheetnames),'Invalid workbook.');workbook.close()
                exports.append(detail)
            context=build_analytics_context(user,date(2026,10,1),date(2026,11,30),'custom')
            # Independent source aggregation: notes have matching credit/debit values in this dataset.
            confirmed=list(Invoice.objects.filter(user=user,status__in=['verified','paid'],invoice_date__range=[date(2026,10,1),date(2026,11,30)]))
            for kind,key in [('sales','sales'),('purchase','purchases'),('expense','expenses')]:
                expected=sum((i.total_amount for i in confirmed if i.invoice_type==kind),D('0'))
                require(context['totals'][key]==expected,'Dashboard total disagrees with independent source sum.')
            exports.append({'dashboard_owner':user.username,'totals':{k:str(v) for k,v in context['totals'].items()}})
        # New provisional actions are previewed only; leave examples staged for manual testing.
        owner_client=Client();owner_client.force_login(owners[0])
        other_client=Client();other_client.force_login(owners[1])
        for sample in inventory.get('invoice_management_examples',[]):
            invoice=Invoice.objects.get(pk=sample['id'],user=owners[0])
            route='invoice_restore' if invoice.status=='cancelled' else 'invoice_change_type'
            content(get(owner_client,route,[invoice.pk]))
            content(get(other_client,route,[invoice.pk],expected=404))
            body=content(get(owner_client,'invoice_detail',[invoice.pk])).decode()
            require('Possible incorrect invoice type' in body,'Synthetic classification warning is missing.')
        # A reviewer has the actual supported audit permission, not a fictitious business membership.
        reviewer=get_user_model().objects.get(username='security-reviewer');client=Client();client.force_login(reviewer)
        content(get(client,'security_monitor'));content(get(client,'security_rules'))
        require(not reviewer.is_superuser and reviewer.has_perm('audit.manage_security'),'Reviewer permission scope changed.')
        for fmt in ('csv','xlsx'):
            response=get(client,'security_export',[fmt]);data=content(response);require(len(data)>50,'Empty security export.')
            exports.append({'route':'security_export','format':fmt,'bytes':len(data)})
        # Use a temporary random credential, restore the old hash even on failure. Nothing is printed.
        owner=owners[0];old=owner.password;password=secrets.token_urlsafe(32)
        try:
            owner.set_password(password);owner.save(update_fields=['password'])
            primary=Client();secondary=Client()
            for browser in (primary,secondary):
                require(browser.post(reverse('login'),{'email':owner.email,'password':password}).status_code==302,'Demo login form failed.')
                content(get(browser,'security_sessions'))
            target=SessionSecurityRecord.objects.get(token_digest=session_digest(secondary.session.session_key))
            response=primary.post(reverse('security_sessions'),{'session_id':target.pk,'password':password})
            require(response.status_code==302,'Session revocation request failed.');target.refresh_from_db()
            require(target.revoked_at is not None,'Revocation history missing.')
            require(secondary.get(reverse('analytics_dashboard')).status_code in (302,403),'Revoked session remained authenticated.')
            content(get(primary,'analytics_dashboard'))
        finally:
            owner.password=old;owner.save(update_fields=['password'])
        require(before==financial_snapshot(),'Verification unexpectedly changed financial records.')
        require(connection.cursor().execute('pragma integrity_check').fetchall()==[('ok',)],'Demo SQLite integrity failed.')
        require(not connection.cursor().execute('pragma foreign_key_check').fetchall(),'Demo foreign-key violations.')
        result={'checks':checks,'exports':exports,'session_revocation':True,'financial_records_unchanged':True,
            'sqlite_integrity':'ok','foreign_key_violations':0,'counts':counts(),
            'limitations':['No native browser interaction, printing or spreadsheet-app inspection.','OCR results remain unconfirmed for mandatory human review.','Passwords must be set locally before manual login.']}
        (root/'verification.json').write_text(json.dumps(result,indent=2))
        self.stdout.write(f'Demo verified: {len(checks)} route checks, exports parsed, session revocation passed; financial records unchanged. See .demo/verification.json.')
