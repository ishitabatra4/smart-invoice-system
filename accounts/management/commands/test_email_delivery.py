from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.core.mail import EmailMultiAlternatives

from accounts.emailing import deliver_message


class Command(BaseCommand):
    help = "Send one development test message using the active SMTP settings."

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="Recipient email address")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("This diagnostic command is available only with DEBUG=true.")
        if settings.EMAIL_BACKEND != "django.core.mail.backends.smtp.EmailBackend":
            raise CommandError("The active email backend is not SMTP. Configure EMAIL_BACKEND before testing.")
        try:
            validate_email(options["to"])
            validate_email(settings.DEFAULT_FROM_EMAIL)
        except ValidationError:
            raise CommandError("Set a valid recipient and DEFAULT_FROM_EMAIL.") from None
        if settings.EMAIL_HOST == "smtp.gmail.com" and (not settings.EMAIL_HOST_USER or not settings.EMAIL_HOST_PASSWORD):
            raise CommandError("Set EMAIL_HOST_USER and EMAIL_HOST_PASSWORD locally before testing Gmail.")
        message = EmailMultiAlternatives(
            "Smart Invoice — SMTP delivery test",
            "This is a single SMTP configuration test from Smart Invoice. No account action is required.",
            settings.DEFAULT_FROM_EMAIL, [options["to"]],
        )
        if not deliver_message(message):
            raise CommandError("SMTP did not confirm acceptance. Check credentials, TLS/SSL, connection and timeout settings. No inbox delivery is claimed.")
        self.stdout.write(self.style.SUCCESS("SMTP backend accepted one test message. Inbox delivery is not guaranteed; check inbox and spam."))
