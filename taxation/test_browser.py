"""Opt-in native Safari acceptance. No third-party browser dependency or live data."""
import base64
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from . import test_preparation as fixtures
from invoices import test_manual
from .models import TaxWorkingPaper


@unittest.skipUnless(os.environ.get('RUN_SAFARI_ACCEPTANCE') == '1', 'Opt-in Safari browser run requires approved local SafariDriver; see Phase 12 report.')
@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', SESSION_COOKIE_NAME='smart_invoice_safari_test_session', CSRF_COOKIE_NAME='smart_invoice_safari_test_csrf', SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False, SECURE_SSL_REDIRECT=False)
class SafariAcceptanceTests(StaticLiveServerTestCase):
    host = '127.0.0.1'
    payload = test_manual.ManualInvoiceTests.payload
    create = test_manual.ManualInvoiceTests.create

    def command(self, method, path, data=None):
        request = Request(self.driver_url + path, data=json.dumps(data).encode() if data is not None else None, method=method, headers={'Content-Type':'application/json'})
        try:
            with urlopen(request, timeout=40) as response:
                value = json.load(response)['value']
        except HTTPError as exc:
            detail = json.load(exc).get('value', {})
            raise RuntimeError('SafariDriver: ' + str(detail.get('error', 'unavailable')) + ' — ' + str(detail.get('message', 'No diagnostic available.'))) from None
        return value

    def session(self, method, path, data=None):
        return self.command(method, '/session/' + self.session_id + path, data)

    def script(self, text, *args):
        return self.session('POST', '/execute/sync', {'script':text, 'args':list(args)})

    def wait(self, script):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self.script(script):
                return
            time.sleep(.15)
        self.fail('Browser workflow did not reach its expected state; no automatic submission retry.')

    def click(self, selector):
        element = self.session('POST', '/element', {'using':'css selector', 'value':selector})
        self.session('POST', '/element/' + element['element-6066-11e4-a52e-4f735466cecf'] + '/click', {})

    def fill(self, selector, value):
        self.script('document.querySelector(arguments[0]).value=arguments[1];', selector, value)

    def capture(self, name):
        data = self.session('GET', '/screenshot')
        (self.evidence / (name + '.png')).write_bytes(base64.b64decode(data))

    def login(self, email):
        self.session('POST', '/url', {'url':self.live_server_url + '/accounts/login/'})
        self.fill('#id_email', email);self.fill('#id_password', 'secret')
        self.script('document.querySelector("#id_email").form.requestSubmit();')
        self.wait('return location.pathname === "/analytics/";')

    def test_isolated_browser_workflow(self):
        test_manual.ManualInvoiceTests.setUp(self)
        self.business.business_name='Synthetic Safari Acceptance Business';self.business.gstin=fixtures.valid_gstin();self.business.save()
        self.other.email='other-browser@example.test';self.other.save()
        self.invoice=self.create(**{'items-0-quantity':'1','items-0-rate':'1000','items-0-discount':'0'})
        test_manual.ManualInvoiceTests.finalize(self,self.invoice)
        self.driver_url=os.environ.get('SAFARI_DRIVER_URL','http://127.0.0.1:4445').rstrip('/')
        parsed=urlsplit(self.driver_url)
        self.assertIn(parsed.hostname,('127.0.0.1','localhost'));self.assertEqual(parsed.scheme,'http')
        root=Path(os.environ.get('SAFARI_EVIDENCE_DIR',tempfile.gettempdir()));self.evidence=root/('safari-'+uuid4().hex);self.evidence.mkdir(parents=True)
        self.session_id=self.command('POST','/session',{'capabilities':{'alwaysMatch':{'browserName':'safari'}}})['sessionId']
        try:
            self.session('POST','/timeouts',{'script':30000,'pageLoad':30000,'implicit':0})
            self.login(self.user.email)
            self.assertEqual(self.script('return document.querySelector("h1").textContent;'),'Business overview')
            self.assertIn(self.business.business_name,self.script('return document.querySelector(".workspace-context").textContent;'))
            self.session('POST','/window/rect',{'width':1280,'height':900});self.capture('desktop-dashboard')
            self.session('POST','/window/rect',{'width':600,'height':900});viewport=self.script('return innerWidth;');self.capture('narrow-dashboard')
            self.session('POST','/window/rect',{'width':1280,'height':900})
            self.session('POST','/url',{'url':self.live_server_url+'/tax/prepare/'})
            for key,value in [('kind','monthly'),('start_date','2026-10-01'),('end_date','2026-10-31')]:self.fill('#id_'+key,value)
            self.script('document.querySelector("#id_kind").form.requestSubmit();');self.wait('return /prepare\\/period\\/\\d+\\/$/.test(location.pathname);')
            self.click('a[href$="/gstr1/"]');self.wait('return !!document.querySelector("#id_aato_band");')
            self.click('button[value="preview"]');self.wait('return document.body.textContent.includes("Confirm standard domestic scope");')
            self.assertEqual(TaxWorkingPaper.objects.count(),0)
            self.script('document.querySelector("#id_ordinary_domestic").checked=true;')
            self.fill('#id_aato_band','up_to_5cr');self.fill('#id_registration_'+str(self.invoice.pk),'unregistered');self.fill('#id_reason_'+str(self.invoice.pk),'Synthetic independent registration evidence')
            self.click('button[value="preview"]');self.wait('return !!document.querySelector("input[name=preview_token]");')
            self.script('document.querySelector("#id_confirmed").checked=true;');self.click('button[value="prepare"]');self.wait('return /prepare\\/paper\\/\\d+\\/$/.test(location.pathname);')
            paper=TaxWorkingPaper.objects.get();self.assertEqual(paper.data['included'][0]['values']['total'],'1180.00');original=paper.data;paper_url='/tax/prepare/paper/'+str(paper.pk)+'/'
            self.fill('#status','reviewed');self.fill('#reason','Synthetic browser review');self.script('document.querySelector("#reason").form.requestSubmit();');self.wait('return document.body.textContent.includes("reviewed");');self.capture('reviewed-working-paper')
            downloads=[]
            for fmt in ('csv','xlsx','pdf'):
                url=paper_url+'export/'+fmt+'/'
                result=self.session('POST','/execute/async',{'script':'const done=arguments[arguments.length-1]; fetch(arguments[0],{credentials:"same-origin"}).then(async r=>{const bytes=new Uint8Array(await r.arrayBuffer());done({status:r.status,size:bytes.length,data:btoa(Array.from(bytes,b=>String.fromCharCode(b)).join(""))});}).catch(()=>done({status:0}));','args':[url]})
                self.assertEqual(result['status'],200);content=base64.b64decode(result['data']);self.assertGreater(len(content),100);(self.evidence/('working-paper.'+fmt)).write_bytes(content);downloads.append({'format':fmt,'bytes':len(content),'method':'authenticated browser fetch; download manager not tested'})
            self.click('a[href*="?revise="]');self.wait('return !!document.querySelector("#id_confirmed");');self.click('button[value="preview"]');self.wait('return !!document.querySelector("input[name=preview_token]");');self.script('document.querySelector("#id_confirmed").checked=true;');self.click('button[value="prepare"]');self.wait('return /prepare\\/paper\\/\\d+\\/$/.test(location.pathname);')
            self.assertEqual(TaxWorkingPaper.objects.count(),2);paper.refresh_from_db();self.assertEqual(paper.data,original);self.assertEqual(paper.status,'superseded')
            self.click('form[action$="/accounts/logout/"] button');self.login(self.other.email)
            denied=self.session('POST','/execute/async',{'script':'const done=arguments[arguments.length-1];fetch(arguments[0]).then(r=>done(r.status));','args':[paper_url+'export/pdf/']});self.assertEqual(denied,404)
            (self.evidence/'result.json').write_text(json.dumps({'accepted':['login','workspace identity','navigation','validation','prepare','review','revision','frozen values','owner denial','authenticated export fetch'],'narrow_desktop_viewport':viewport,'downloads':downloads,'pending':['physical print','independent GST review','target spreadsheet apps','native browser download manager','real mobile device']},indent=2))
        finally:
            self.command('DELETE','/session/'+self.session_id)
