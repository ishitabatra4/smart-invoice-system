"""Read-only, local operator diagnostics; never a repair/restore/migrate command."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.state import ProjectState


class Command(BaseCommand):
    help = 'Read-only SQLite integrity, schema/migration and optional protected-manifest verification. No credentials or record contents are printed.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--database-path', help='Optional on-disk SQLite copy; otherwise the configured development database.')
        parser.add_argument('--manifest', help='Selected JSON manifest, version 1 or an existing verification baseline.')
        parser.add_argument('--section', choices=['files', 'protected', 'all'], default='files')
        parser.add_argument('--production', action='store_true', help='Also fail on unsafe HTTPS/debug configuration; does not certify production readiness.')

    def handle(self, *args, **options):
        result = {'read_only': True, 'checks': {}, 'warnings': []}
        errors = []
        database = options.get('database_path') or settings.DATABASES['default']['NAME']
        try:
            if settings.DATABASES['default']['ENGINE'] != 'django.db.backends.sqlite3' or str(database).startswith('file:') or str(database) == ':memory:':
                raise ValueError
            path = Path(database).resolve(strict=True)
            if not path.is_file():
                raise ValueError
            connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
            try:
                connection.execute('PRAGMA query_only=ON')
                valid = connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
                result['checks']['database_integrity'] = 'ok' if valid else 'failed'
                if not valid:
                    errors.append('database_integrity')
                fk_count = len(connection.execute('PRAGMA foreign_key_check').fetchall())
                result['checks']['foreign_key_violations'] = fk_count
                if fk_count:
                    errors.append('foreign_keys')
                applied = set(connection.execute('SELECT app,name FROM django_migrations'))
                loader = MigrationLoader(None)
                disk = set(loader.disk_migrations)
                pending, unknown = disk - applied, applied - disk
                result['checks']['pending_migrations'] = len(pending)
                result['checks']['unknown_migrations'] = len(unknown)
                if pending or unknown:
                    errors.append('migration_history')
                drift = bool(MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps)).changes(graph=loader.graph))
                result['checks']['model_drift'] = drift
                if drift:
                    errors.append('model_drift')
                table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                bad_tables = 0
                for model in apps.get_models(include_auto_created=True):
                    if model._meta.proxy or not model._meta.managed:
                        continue
                    table = model._meta.db_table
                    columns = {row[1] for row in connection.execute('PRAGMA table_info("' + table.replace('"', '""') + '")')}
                    if table not in table_names or columns != {field.column for field in model._meta.local_fields}:
                        bad_tables += 1
                result['checks']['table_column_mismatches'] = bad_tables
                if bad_tables:
                    errors.append('schema_presence')
            finally:
                connection.close()
        except (OSError, ValueError, sqlite3.DatabaseError):
            errors.append('database_unreadable_or_invalid')
        if options.get('manifest'):
            try:
                result['checks']['manifest'] = self.manifest(options['manifest'], options['section'])
                if result['checks']['manifest']['missing'] or result['checks']['manifest']['mismatched']:
                    errors.append('manifest_files')
            except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError):
                errors.append('invalid_manifest')
        else:
            result['warnings'].append('Protected-file hashes not checked: select a trusted manifest.')
        production_safe = not settings.DEBUG and settings.SESSION_COOKIE_SECURE and settings.CSRF_COOKIE_SECURE and settings.SECURE_SSL_REDIRECT and bool(settings.ALLOWED_HOSTS) and '*' not in settings.ALLOWED_HOSTS
        result['checks']['https_debug_configuration'] = 'production_flags_enabled' if production_safe else 'development_or_incomplete'
        if not production_safe:
            result['warnings'].append('Configuration is not verified for production; no secrets were printed.')
            if options['production']:
                errors.append('production_configuration')
        result['errors'] = sorted(set(errors))
        result['ok'] = not errors
        self.stdout.write(json.dumps(result, indent=2))
        if errors:
            raise CommandError('Operational verification failed; inspect the sanitized error codes above.')

    def manifest(self, filename, section):
        path = Path(filename)
        if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError
        with path.open(encoding='utf-8') as source:
            data = json.load(source)
        if not isinstance(data, dict) or data.get('version', 1) != 1:
            raise ValueError
        selected = [section] if section != 'all' else ['files', 'protected']
        files = {}
        for key in selected:
            values = data.get(key)
            if not isinstance(values, dict):
                raise ValueError
            for name, digest in values.items():
                if name in files and files[name] != digest:
                    raise ValueError
                files[name] = digest
        if len(files) > 10000:
            raise ValueError
        root = Path(settings.BASE_DIR).resolve()
        allowed = [Path(settings.MEDIA_ROOT).resolve(), Path(settings.PRIVATE_EXPENSE_ROOT).resolve(), root / '.backups']
        missing = mismatched = 0
        for name, expected in files.items():
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or not relative.parts or not isinstance(expected, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', expected):
                raise ValueError
            target = root / relative
            # No manifest may redirect a check to credentials or arbitrary paths.
            if any(parent.is_symlink() for parent in [target, *target.parents] if parent != root and root in parent.parents):
                raise ValueError
            resolved = target.resolve()
            if not any(resolved.is_relative_to(base) for base in allowed):
                raise ValueError
            if not target.exists():
                missing += 1
                continue
            if not target.is_file():
                raise ValueError
            fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError
                digest = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest().lower() != expected.lower():
                mismatched += 1
        return {'checked': len(files), 'missing': missing, 'mismatched': mismatched}
