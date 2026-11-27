"""Email transport with allow-listed diagnostics and no exception/body logging."""
import logging
import smtplib

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def expiration_description(seconds):
    seconds = int(seconds)
    if seconds % 3600 == 0:
        count = seconds // 3600
        return f"{count} hour" + ("s" if count != 1 else "")
    if seconds % 60 == 0:
        count = seconds // 60
        return f"{count} minute" + ("s" if count != 1 else "")
    return f"{seconds} seconds"


def delivery_error(message):
    """Return an allow-listed error, or empty text for backend acceptance; never log exceptions."""
    try:
        if message.send(fail_silently=False) == 1:
            return ""
        reason = "not_accepted"
    except smtplib.SMTPAuthenticationError:
        reason = "authentication_failed"
    except smtplib.SMTPRecipientsRefused:
        reason = "recipient_rejected"
    except smtplib.SMTPSenderRefused:
        reason = "sender_rejected"
    except smtplib.SMTPDataError:
        reason = "message_rejected"
    except TimeoutError:
        reason = "timeout"
    except smtplib.SMTPException:
        reason = "smtp_rejected"
    except OSError:
        reason = "connection_failed"
    except Exception:
        reason = "delivery_failed"
    return reason


def deliver_message(message):
    reason = delivery_error(message)
    if not reason:
        return True
    logger.warning("Account email was not accepted: %s", reason)
    return False


def send_account_email(subject, text_template, html_template, context, recipient, from_email=None):
    message = EmailMultiAlternatives(
        "".join(subject.splitlines()), render_to_string(text_template, context),
        from_email or settings.DEFAULT_FROM_EMAIL, [recipient],
    )
    if html_template:
        message.attach_alternative(render_to_string(html_template, context), "text/html")
    return deliver_message(message)
