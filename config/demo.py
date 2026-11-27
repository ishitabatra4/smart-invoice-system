"""Local synthetic demo only. Never select these settings for production."""
from .settings import *
from .demo_safety import validate_demo_paths
from django.core.management.utils import get_random_secret_key

ISOLATED_DEMO = True
DEMO_ROOT = BASE_DIR / '.demo'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3',
    'NAME': str(DEMO_ROOT / 'demo.sqlite3'),
    'OPTIONS': {'transaction_mode': 'IMMEDIATE', 'timeout': 20}}}
MEDIA_ROOT = DEMO_ROOT / 'media'
PRIVATE_EXPENSE_ROOT = DEMO_ROOT / 'private'
validate_demo_paths(BASE_DIR, DATABASES['default']['NAME'], MEDIA_ROOT, PRIVATE_EXPENSE_ROOT)
SECRET_KEY = (DEMO_ROOT / 'secret-key').read_text().strip() if (DEMO_ROOT / 'secret-key').exists() else get_random_secret_key()
DEBUG = True
ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]', 'testserver']
SESSION_COOKIE_NAME = 'smart_invoice_demo_session'
CSRF_COOKIE_NAME = 'smart_invoice_demo_csrf'
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_PROXY_SSL_HEADER = None
CSRF_TRUSTED_ORIGINS = ['http://127.0.0.1:8001', 'http://localhost:8001']
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
EMAIL_HOST = EMAIL_HOST_USER = EMAIL_HOST_PASSWORD = ''
DEFAULT_FROM_EMAIL = 'demo@example.test'
EMAIL_TIMEOUT = 5
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'isolated-demo'}}

