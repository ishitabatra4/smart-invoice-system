import hashlib
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch
from django.core.management import call_command, CommandError
from django.db import connection
from django.test import TransactionTestCase, override_settings


class OperationalVerificationTests(TransactionTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup);self.root=Path(self.folder.name)
        self.database=self.root/'copy.sqlite3';connection.ensure_connection();copy=sqlite3.connect(self.database);connection.connection.backup(copy);copy.close()
        self.config=override_settings(BASE_DIR=self.root,MEDIA_ROOT=self.root/'media',PRIVATE_EXPENSE_ROOT=self.root/'private_expense_files');self.config.enable();self.addCleanup(self.config.disable)
        (self.root/'media').mkdir();self.file=self.root/'media'/'synthetic.bin';self.file.write_bytes(b'synthetic original')
        self.manifest=self.root/'manifest.json';self.manifest.write_text(json.dumps({'version':1,'files':{'media/synthetic.bin':hashlib.sha256(self.file.read_bytes()).hexdigest()}}))

    def check(self,**options):
        output=StringIO();call_command('verify_operations',database_path=str(self.database),stdout=output,**options);return json.loads(output.getvalue())

    def test_valid_state_is_byte_for_byte_read_only(self):
        before=self.database.read_bytes();self.assertTrue(self.check(manifest=str(self.manifest))['ok']);self.assertEqual(before,self.database.read_bytes())

    def test_missing_and_corrupted_files(self):
        self.file.write_bytes(b'changed')
        with self.assertRaises(CommandError):self.check(manifest=str(self.manifest))
        self.file.unlink()
        with self.assertRaises(CommandError):self.check(manifest=str(self.manifest))

    def test_invalid_manifest_and_path_escape(self):
        for data in [[],{'version':2,'files':{}},{'files':{'../.env':'0'*64}},{'files':{'media/file':'wrong'}},{'files':{'config/settings.py':'0'*64}}]:
            self.manifest.write_text(json.dumps(data))
            with self.subTest(data=data),self.assertRaises(CommandError):self.check(manifest=str(self.manifest))

    def test_symlink_manifest_cannot_follow_outside_storage(self):
        self.file.unlink();self.file.symlink_to(self.manifest)
        with self.assertRaises(CommandError):self.check(manifest=str(self.manifest))

    def test_unreadable_and_corrupt_databases_fail_without_creation(self):
        missing=self.root/'missing.sqlite3'
        with self.assertRaises(CommandError):call_command('verify_operations',database_path=str(missing),stdout=StringIO())
        self.assertFalse(missing.exists());self.database.write_bytes(b'not sqlite')
        with self.assertRaises(CommandError):self.check()

    def test_migration_mismatch(self):
        c=sqlite3.connect(self.database);c.execute("DELETE FROM django_migrations WHERE app='taxation' AND name LIKE '0002%'");c.commit();c.close()
        with self.assertRaises(CommandError):self.check()

    def test_schema_mismatch(self):
        c=sqlite3.connect(self.database);c.execute('DROP TABLE taxation_taxworkingpaperevent');c.commit();c.close()
        with self.assertRaises(CommandError):self.check()

    def test_foreign_key_failure(self):
        c=sqlite3.connect(self.database);c.execute("INSERT INTO taxation_taxperiod(kind,start_date,end_date,created_at,business_id,created_by_id) VALUES('monthly','2026-01-01','2026-01-31','2026-01-01',9999,9999)");c.commit();c.close()
        with self.assertRaises(CommandError):self.check()

    def test_integrity_failure(self):
        # Corrupt a temporary copy, never the configured development database.
        with self.database.open('r+b') as target:target.write(b'corrupt database header')
        with self.assertRaises(CommandError):self.check()

    def test_model_drift_failure(self):
        with patch('taxation.management.commands.verify_operations.MigrationAutodetector.changes',return_value={'taxation':[object()]}):
            with self.assertRaises(CommandError):self.check()

    def test_no_credentials_in_output_and_production_flags(self):
        with override_settings(DEBUG=True,EMAIL_HOST_PASSWORD='SECRET_MUST_NOT_APPEAR'):
            result=self.check();self.assertNotIn('SECRET_MUST_NOT_APPEAR',json.dumps(result))
            with self.assertRaises(CommandError):self.check(production=True)
