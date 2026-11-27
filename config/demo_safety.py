"""Path checks run before migration, seeding or serving demo records."""
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured


def validate_demo_paths(base, database, public, private):
    base = Path(base).resolve()
    root = base / '.demo'
    expected = (root / 'demo.sqlite3', root / 'media', root / 'private')
    for actual, wanted in zip((database, public, private), expected):
        path = Path(actual).absolute()
        if path != wanted or path.resolve() != wanted:
            raise ImproperlyConfigured('Demo database and storage must use separate, non-symlinked .demo paths.')
    return root


def require_demo():
    from django.conf import settings
    from django.core.files.storage import default_storage
    from expenses.models import private_expense_storage
    if not getattr(settings, 'ISOLATED_DEMO', False) or settings.SETTINGS_MODULE != 'config.demo':
        raise ImproperlyConfigured('Use --settings=config.demo; development/production seeding is forbidden.')
    if settings.DATABASES['default']['ENGINE'] != 'django.db.backends.sqlite3':
        raise ImproperlyConfigured('Demo requires SQLite.')
    root = validate_demo_paths(settings.BASE_DIR, settings.DATABASES['default']['NAME'], settings.MEDIA_ROOT, settings.PRIVATE_EXPENSE_ROOT)
    if (settings.EMAIL_BACKEND != 'django.core.mail.backends.locmem.EmailBackend'
        or settings.SESSION_COOKIE_NAME != 'smart_invoice_demo_session'
        or Path(default_storage.location).resolve() != root / 'media'
        or Path(private_expense_storage.location).resolve() != root / 'private'):
        raise ImproperlyConfigured('Demo email, session or active storage isolation failed.')
    return root
