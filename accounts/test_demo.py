"""Isolation guards must fail before commands can write to protected paths."""
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, override_settings
from config.demo_safety import validate_demo_paths, require_demo
from accounts.demo_data import gstin
from invoices.gstin import gstin_evidence


class DemoSafetyTests(SimpleTestCase):
    def paths(self, base):
        root=Path(base).resolve()/'.demo'
        return [root/'demo.sqlite3',root/'media',root/'private']

    def test_exact_isolated_paths_accepted(self):
        with TemporaryDirectory() as base:
            self.assertEqual(validate_demo_paths(base,*self.paths(base)),Path(base).resolve()/'.demo')

    def test_development_database_rejected(self):
        with TemporaryDirectory() as base:
            paths=self.paths(base);paths[0]=Path(base)/'db.sqlite3'
            with self.assertRaises(ImproperlyConfigured): validate_demo_paths(base,*paths)

    def test_protected_public_and_private_storage_rejected(self):
        with TemporaryDirectory() as base:
            for index,name in [(1,'media'),(2,'private_expense_files'),(2,'.backups')]:
                paths=self.paths(base);paths[index]=Path(base)/name
                with self.assertRaises(ImproperlyConfigured): validate_demo_paths(base,*paths)

    def test_symlinked_root_rejected(self):
        with TemporaryDirectory() as base, TemporaryDirectory() as foreign:
            (Path(base)/'.demo').symlink_to(foreign,target_is_directory=True)
            with self.assertRaises(ImproperlyConfigured):validate_demo_paths(base,*self.paths(base))

    def test_symlinked_database_and_storage_rejected(self):
        with TemporaryDirectory() as base:
            root=Path(base)/'.demo';root.mkdir();foreign=Path(base)/'protected';foreign.mkdir()
            (root/'media').symlink_to(foreign,target_is_directory=True)
            with self.assertRaises(ImproperlyConfigured):validate_demo_paths(base,*self.paths(base))

    def test_development_command_refused_before_directory_or_db_access(self):
        with patch('pathlib.Path.mkdir') as mkdir:
            with self.assertRaises(CommandError):call_command('setup_demo',stdout=StringIO())
            mkdir.assert_not_called()

    @override_settings(ISOLATED_DEMO=True,SETTINGS_MODULE='config.settings')
    def test_flag_alone_cannot_authorize_seeding(self):
        with self.assertRaises(ImproperlyConfigured):require_demo()

    @override_settings(ISOLATED_DEMO=True,SETTINGS_MODULE='config.demo',EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend')
    def test_smtp_or_wrong_storage_rejected(self):
        with self.assertRaises(ImproperlyConfigured):require_demo()

    def test_fictional_identifiers_pass_local_checksum(self):
        for prefix in ('27ABCDE8100F1Z','27ABCDE8101F1Z','27ABCDE8200F1Z','27ABCDE8300F1Z'):
            self.assertTrue(gstin_evidence(gstin(prefix))['checksum_valid'])


from django.test import TestCase
from django.contrib.auth import get_user_model
from accounts.models import BusinessProfile, BusinessParty
from accounts.demo_data import add_overdue_example
from invoices.models import Invoice, InvoiceNumberSequence
from invoices.ledger import invoice_outstanding
from datetime import date
from decimal import Decimal


class DemoOverdueTests(TestCase):
    def setUp(self):
        user=get_user_model().objects.create_user('lumen-owner',email='lumen-owner@example.test')
        business=BusinessProfile.objects.create(user=user,business_name='Lumen Demo Trading',gstin=gstin('27ABCDE8100F1Z'))
        BusinessParty.objects.create(business=business,name='Fictional demo customer',party_type='customer')

    @patch('accounts.demo_data.require_demo')
    def test_overdue_example_uses_saved_gst_and_is_idempotent(self,guard):
        inventory={'invoices':[]};add_overdue_example(inventory);add_overdue_example(inventory)
        invoice=Invoice.objects.get();self.assertEqual(invoice.document_state,'finalized')
        self.assertEqual(invoice.due_date,date(2026,10,31))
        self.assertEqual((invoice.taxable_amount,invoice.cgst_amount,invoice.sgst_amount,invoice.total_amount),tuple(map(Decimal,('1000','90','90','1180'))))
        self.assertEqual(invoice_outstanding(invoice),Decimal('1180'))
        self.assertEqual(len(inventory['invoices']),1);self.assertEqual(InvoiceNumberSequence.objects.get().next_value,2)
        self.assertEqual(inventory['invoices'][0]['number'],invoice.invoice_number)

    @patch('accounts.demo_data.require_demo')
    def test_lost_inventory_retry_does_not_duplicate_invoice(self,guard):
        add_overdue_example({'invoices':[]});recovered={'invoices':[]};add_overdue_example(recovered)
        self.assertEqual(Invoice.objects.count(),1);self.assertEqual(InvoiceNumberSequence.objects.get().next_value,2)
        self.assertEqual(recovered['invoices'][0]['id'],Invoice.objects.get().pk)


class DemoAcceptanceExamplesTests(TestCase):
    from invoices import test_manual as _manual_fixtures
    setUp = _manual_fixtures.ManualInvoiceTests.setUp
    payload = _manual_fixtures.ManualInvoiceTests.payload
    create = _manual_fixtures.ManualInvoiceTests.create
    finalize = _manual_fixtures.ManualInvoiceTests.finalize

    @patch('accounts.demo_data.require_demo')
    def test_lifecycle_extension_is_idempotent_and_preserves_recognized_invoices(self, guard):
        from accounts.demo_data import add_acceptance_examples
        from invoices.models import CommercialDocument, InvoiceAdjustmentNote
        self.user.username='lumen-owner';self.user.save()
        source=self.create();self.finalize(source);source.refresh_from_db()
        before=Invoice.objects.filter(status__in=['verified','paid']).values().get()
        inventory={};add_acceptance_examples(inventory)
        self.assertEqual(CommercialDocument.objects.count(),10)
        self.assertEqual(InvoiceAdjustmentNote.objects.count(),4)
        self.assertEqual(set(CommercialDocument.objects.values_list('status',flat=True)),{'draft','rejected','cancelled','converted'})
        self.assertEqual(set(InvoiceAdjustmentNote.objects.values_list('status',flat=True)),{'draft','cancelled'})
        self.assertEqual(Invoice.objects.filter(document_state='draft').count(),2)
        self.assertEqual(Invoice.objects.filter(status__in=['verified','paid']).values().get(),before)
        financial=list(Invoice.objects.order_by('pk').values());add_acceptance_examples(inventory)
        self.assertEqual(list(Invoice.objects.order_by('pk').values()),financial)
        # Deterministic creation keys recover a lost manifest without extra records.
        add_acceptance_examples({})
        self.assertEqual(CommercialDocument.objects.count(),10);self.assertEqual(InvoiceAdjustmentNote.objects.count(),4)
        self.assertEqual(list(Invoice.objects.order_by('pk').values()),financial)

    def test_extension_is_forbidden_outside_demo(self):
        from accounts.demo_data import add_acceptance_examples
        with self.assertRaises(ImproperlyConfigured):add_acceptance_examples({})


class InvoiceManagementDemoTests(TestCase):
    from invoices import test_manual as _manual_fixtures
    setUp = _manual_fixtures.ManualInvoiceTests.setUp

    @patch('accounts.demo_data.require_demo')
    def test_matching_management_examples_are_unconfirmed_and_repeatable(self,guard):
        from accounts.demo_data import add_invoice_management_examples
        from invoices.classification import classification_assessment
        from tempfile import TemporaryDirectory
        from django.test import override_settings
        self.user.username='lumen-owner';self.user.save();self.business.state='Maharashtra';self.business.save()
        inventory={}
        with TemporaryDirectory() as media,override_settings(MEDIA_ROOT=media):
            add_invoice_management_examples(inventory);before=list(Invoice.objects.order_by('pk').values())
            self.assertEqual(len(before),3);self.assertEqual(Invoice.objects.filter(status='cancelled').count(),1)
            self.assertFalse(Invoice.objects.filter(status__in=['verified','paid']).exists())
            for invoice in Invoice.objects.all():
                attempt=invoice.extraction_attempts.first()
                self.assertEqual(classification_assessment(self.business,attempt.result,'sales')['status'],'wrong_type')
            add_invoice_management_examples(inventory);add_invoice_management_examples({})
            self.assertEqual(list(Invoice.objects.order_by('pk').values()),before)

    def test_management_examples_refuse_original_settings(self):
        from accounts.demo_data import add_invoice_management_examples
        with self.assertRaises(ImproperlyConfigured):add_invoice_management_examples({})
