"""Small literal KEY=value loader; process environment always takes precedence."""
import os
import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


def load_local_env(path, environ=None):
    environ = os.environ if environ is None else environ
    path = Path(path)
    if not path.is_file():
        return
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ImproperlyConfigured(f"Invalid environment assignment on line {line_number}; use KEY=value.")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ImproperlyConfigured(f"Unclosed environment quote on line {line_number}.")
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        # Literal values: no shell execution, expansion, escape interpretation or secret logging.
        environ.setdefault(key, value)


def email_settings(environ):
    def boolean(name, default):
        value = environ.get(name, default).strip().lower()
        if value not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ImproperlyConfigured(f"{name} must be a boolean.")
        return value in {"true", "1", "yes", "on"}

    def positive_int(name, default):
        try:
            value = int(environ.get(name, default))
            if value <= 0:
                raise ValueError
            return value
        except (TypeError, ValueError):
            raise ImproperlyConfigured(f"{name} must be a positive integer.") from None

    use_tls = boolean("EMAIL_USE_TLS", "true")
    use_ssl = boolean("EMAIL_USE_SSL", "false")
    if use_tls and use_ssl:
        raise ImproperlyConfigured("EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be enabled.")
    backend = environ.get("EMAIL_BACKEND", "django.core.mail.backends.locmem.EmailBackend").strip()
    if not backend:
        raise ImproperlyConfigured("EMAIL_BACKEND cannot be blank.")
    user = environ.get("EMAIL_HOST_USER", "").strip()
    return {
        "EMAIL_BACKEND": backend,
        "EMAIL_HOST": environ.get("EMAIL_HOST", "localhost").strip(),
        "EMAIL_PORT": positive_int("EMAIL_PORT", "465" if use_ssl else "587"),
        "EMAIL_HOST_USER": user,
        "EMAIL_HOST_PASSWORD": environ.get("EMAIL_HOST_PASSWORD", ""),
        "EMAIL_USE_TLS": use_tls,
        "EMAIL_USE_SSL": use_ssl,
        "EMAIL_TIMEOUT": positive_int("EMAIL_TIMEOUT", "15"),
        "DEFAULT_FROM_EMAIL": environ.get("DEFAULT_FROM_EMAIL", user or "noreply@localhost").strip(),
    }
