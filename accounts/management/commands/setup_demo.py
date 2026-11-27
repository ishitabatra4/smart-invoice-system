"""Create only the explicitly isolated local demo; never seed development."""
import getpass
import json
import os
import sys
from django.core.management import BaseCommand, CommandError, call_command
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.contrib.auth import get_user_model, password_validation
from config.demo_safety import require_demo


class Command(BaseCommand):
    help = 'Initialize synthetic .demo data with --settings=config.demo; local passwords use a hidden prompt.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--set-password', choices=['lumen-owner', 'cobalt-owner', 'security-reviewer'])
        parser.add_argument('--complete-acceptance', action='store_true', help='Append idempotent fictional lifecycle examples in the isolated demo only.')
        parser.add_argument('--invoice-management', action='store_true', help='Stage three unconfirmed type-correction/restoration examples in the isolated demo only.')

    def handle(self, *args, **options):
        try:
            root = require_demo()
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from None
        root.mkdir(mode=0o700, exist_ok=True)
        for directory in ('media', 'private'):
            (root / directory).mkdir(mode=0o700, exist_ok=True)
        secret = root / 'secret-key'
        if not secret.exists():
            with secret.open('x') as stream:
                os.chmod(secret, 0o600)
                stream.write(settings.SECRET_KEY)
        from django.db import connection
        tables = connection.introspection.table_names()
        manifest = root / 'inventory.json'
        if options['set_password']:
            if not manifest.exists():
                raise CommandError('Run initial demo setup first.')
            if not sys.stdin.isatty():
                raise CommandError('Password setup requires an interactive local terminal; never pass credentials as arguments.')
            user = get_user_model().objects.get(username=options['set_password'])
            first = getpass.getpass('New local demo password: ')
            second = getpass.getpass('Repeat local demo password: ')
            if first != second:
                raise CommandError('Passwords did not match; nothing changed.')
            try:
                password_validation.validate_password(first, user)
            except ValidationError as exc:
                raise CommandError(' '.join(exc.messages)) from None
            user.set_password(first)
            user.save(update_fields=['password'])
            self.stdout.write('Local demo password established; no credential was printed.')
            return
        if manifest.exists():
            from accounts.demo_data import verify_inventory, add_acceptance_examples
            data = json.loads(manifest.read_text())
            verify_inventory(data)
            if options['complete_acceptance'] or options['invoice_management']:
                if options['complete_acceptance']:
                    add_acceptance_examples(data)
                if options['invoice_management']:
                    from accounts.demo_data import add_invoice_management_examples
                    add_invoice_management_examples(data)
                temporary = manifest.with_suffix('.json.tmp')
                temporary.write_text(json.dumps(data, indent=2, default=str))
                temporary.replace(manifest)
                self.stdout.write('Isolated acceptance lifecycle examples verified. No existing financial examples were finalized or reset.')
                return
            self.stdout.write('Existing demo verified. No records inserted or reset. See .demo/inventory.json.')
            return
        if tables:
            from accounts.demo_data import counts
            from django.db.migrations.executor import MigrationExecutor
            executor = MigrationExecutor(connection)
            if any(counts().values()) or executor.migration_plan(executor.loader.graph.leaf_nodes()):
                raise CommandError('Existing demo has data or an incomplete schema but no inventory. Refusing repair/reset; see recovery instructions.')
        else:
            call_command('migrate', interactive=False, verbosity=0)
        from accounts.demo_data import populate
        try:
            data = populate()
            if options['complete_acceptance']:
                from accounts.demo_data import add_acceptance_examples
                add_acceptance_examples(data)
            if options['invoice_management']:
                from accounts.demo_data import add_invoice_management_examples
                add_invoice_management_examples(data)
        except ValidationError as exc:
            raise CommandError('Demo validation stopped safely: ' + ' '.join(exc.messages)) from None
        except Exception:
            raise CommandError('Demo setup did not complete. Isolated files were retained; development was not touched. Use documented recovery; no automatic retry/reset.') from None
        with manifest.open('x') as stream:
            json.dump(data, stream, indent=2, default=str)
        self.stdout.write(self.style.SUCCESS('Isolated demo created. No usable passwords are stored initially.'))
        self.stdout.write('Set each password locally with setup_demo --set-password <username> --settings=config.demo.')
        self.stdout.write('Actual IDs, numbers, checkpoints and mappings: .demo/inventory.json')
