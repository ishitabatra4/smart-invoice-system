"""Keep recovery and verification credentials out of Django request logs."""
import logging
import re


def redact_account_tokens(value):
    value = re.sub(r'(/accounts/(?:reset|verify)/)[^\s?"<>]+', r"\1[redacted]", str(value))
    value = re.sub(r'(?i)\bBearer\s+[^\s,;"<>]+', 'Bearer [redacted]', value)
    value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[redacted-jwt]', value)
    value = re.sub(r'(?i)((?:password|passwd|secret|token|api[_-]?key|authorization|sessionid|smtp[_-]?password)\s*[=:]\s*)(?:"[^"\n]*"|\x27[^\x27\n]*\x27|[^\s,;<>]+)', r'\1[redacted]', value)
    value = re.sub(r'(?i)(["\'](?:password|passwd|secret|token|api[_-]?key|authorization|sessionid)["\']\s*:\s*)(?:"[^"\n]*"|\'[^\'\n]*\')', r'\1"[redacted]"', value)
    return value


class RedactAccountTokens(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        redacted = redact_account_tokens(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True
