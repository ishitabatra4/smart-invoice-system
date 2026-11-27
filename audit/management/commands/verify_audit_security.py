"""Read-only chain verification; optional independently protected head comparison."""
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from audit.security import record_digest
EVENT_FIELDS = ('id', 'business_id', 'user_id', 'actor_label', 'action', 'module', 'object_type', 'object_id', 'related_object_type', 'related_object_id', 'description', 'before', 'after', 'ip_address', 'user_agent', 'success', 'metadata', 'created_at')
RECORD_FIELDS = ('id', 'event_id', 'sequence', 'event_type', 'severity', 'correlation_id', 'principal', 'network', 'source', 'previous_hash', 'digest', 'created_at')

def moment(value):
    result = datetime.fromisoformat(str(value))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result

class Command(BaseCommand):
    help = 'Read-only verification of new audit seals; stdout head can be retained independently. Historical unsealed rows are not verified.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--database-path')
        parser.add_argument('--checkpoint', help='Trusted external JSON head previously emitted by this command.')

    def handle(self, *args, **options):
        result = {'read_only': True, 'covered_records': 0, 'errors': [], 'scope': 'New sealed events only; a local mutable chain does not defeat a privileged administrator.'}
        checkpoint = None
        try:
            if options.get('checkpoint'):
                file = Path(options['checkpoint'])
                if file.stat().st_size > 4096:
                    raise ValueError
                checkpoint = json.loads(file.read_text())
                if not isinstance(checkpoint, dict):
                    raise ValueError
                checkpoint = checkpoint.get('checkpoint', checkpoint)
                if not isinstance(checkpoint, dict):
                    raise ValueError
                if checkpoint.get('version') != 1 or type(checkpoint.get('last_sequence')) is not int or checkpoint['last_sequence'] < 0 or (not re.fullmatch('[a-f0-9]{64}', checkpoint.get('last_digest', ''))):
                    raise ValueError
            name = options.get('database_path') or settings.DATABASES['default']['NAME']
            if settings.DATABASES['default']['ENGINE'] != 'django.db.backends.sqlite3' or str(name).startswith('file:') or str(name) == ':memory:':
                raise ValueError
            path = Path(name).resolve(strict=True)
            connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            try:
                connection.execute('PRAGMA query_only=ON')
                if connection.execute('pragma integrity_check').fetchall() != [('ok',)] or connection.execute('pragma foreign_key_check').fetchall():
                    raise ValueError
                selected = [f'r."{field}" AS "r_{field}"' for field in RECORD_FIELDS] + [f'e."{field}" AS "e_{field}"' for field in EVENT_FIELDS]
                connection.row_factory = sqlite3.Row
                rows = connection.execute('SELECT ' + ','.join(selected) + ' FROM audit_securityrecord r LEFT JOIN audit_auditevent e ON r.event_id=e.id ORDER BY r.sequence')
                previous = '0' * 64
                sequence = 0
                checkpoint_found = checkpoint is None or (checkpoint['last_sequence'] == 0 and checkpoint['last_digest'] == '0' * 64)
                for row in rows:
                    if row['e_id'] is None:
                        raise ValueError
                    event = SimpleNamespace(**{field: row['e_' + field] for field in EVENT_FIELDS})
                    record = SimpleNamespace(**{field: row['r_' + field] for field in RECORD_FIELDS})
                    for field in ('before', 'after', 'metadata'):
                        setattr(event, field, json.loads(getattr(event, field)))
                    event.success = bool(event.success)
                    event.created_at = moment(event.created_at)
                    record.created_at = moment(record.created_at)
                    if record.correlation_id:
                        from uuid import UUID
                        record.correlation_id = UUID(record.correlation_id)
                    if record.sequence != sequence + 1 or record.previous_hash != previous or record.digest != record_digest(record, event):
                        raise ValueError
                    sequence = record.sequence
                    previous = record.digest
                    if checkpoint and sequence == checkpoint['last_sequence']:
                        checkpoint_found = previous == checkpoint['last_digest']
                if not checkpoint_found:
                    raise ValueError
                result['covered_records'] = sequence
                result['unsealed_events'] = connection.execute('SELECT COUNT(*) FROM audit_auditevent e LEFT JOIN audit_securityrecord r ON r.event_id=e.id WHERE r.id IS NULL').fetchone()[0]
                result['checkpoint'] = {'version': 1, 'last_sequence': sequence, 'last_digest': previous}
            finally:
                connection.close()
        except (OSError, ValueError, TypeError, sqlite3.DatabaseError, KeyError):
            result['errors'].append('unavailable_schema_or_invalid_integrity_or_checkpoint')
        result['ok'] = not result['errors']
        self.stdout.write(json.dumps(result, indent=2))
        if result['errors']:
            raise CommandError('Audit verification failed; no data was changed. Inspect the protected database/checkpoint separately.')
