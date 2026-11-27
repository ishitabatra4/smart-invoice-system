import re
import smtplib
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils.http import urlsafe_base64_decode

from config.environment import email_settings, load_local_env
from .emailing import deliver_message
from .models import AccountVerification, BusinessProfile


class EmailConfigurationTests(SimpleTestCase):
    def test_literal_env_loading_and_process_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / '.env'
            env_file.write_text('# private local config\nEMAIL_HOST=smtp.gmail.com\nEMAIL_HOST_PASSWORD="literal-$value#kept"\nexport DEBUG=True\n')
            values = {'EMAIL_HOST': 'process-host'}
            load_local_env(env_file, values)
            self.assertEqual(values['EMAIL_HOST'], 'process-host')
            self.assertEqual(values['EMAIL_HOST_PASSWORD'], 'literal-$value#kept')
            self.assertEqual(values['DEBUG'], 'True')

    def test_env_errors_do_not_echo_secrets_and_missing_file_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            load_local_env(path, {})
            path.write_text('EMAIL_HOST_PASSWORD="private-unclosed')
            with self.assertRaises(ImproperlyConfigured) as error:
                load_local_env(path, {})
            self.assertNotIn('private-unclosed', str(error.exception))

    def test_smtp_selection_tls_ssl_and_timeout_validation(self):
        values = email_settings({'EMAIL_BACKEND': 'django.core.mail.backends.smtp.EmailBackend',
            'EMAIL_HOST': 'smtp.gmail.com', 'EMAIL_USE_TLS': 'True', 'EMAIL_USE_SSL': 'False', 'EMAIL_TIMEOUT': '12'})
        self.assertEqual(values['EMAIL_BACKEND'], 'django.core.mail.backends.smtp.EmailBackend')
        self.assertTrue(values['EMAIL_USE_TLS'])
        self.assertFalse(values['EMAIL_USE_SSL'])
        self.assertEqual(values['EMAIL_TIMEOUT'], 12)
        for config in ({'EMAIL_USE_TLS': 'true', 'EMAIL_USE_SSL': 'true'}, {'EMAIL_TIMEOUT': '0'}, {'EMAIL_USE_TLS': 'invalid'}, {'EMAIL_PORT': 'invalid'}, {'EMAIL_BACKEND': ''}):
            with self.subTest(config=config), self.assertRaises(ImproperlyConfigured):
                email_settings(config)
        self.assertEqual(email_settings({'EMAIL_USE_TLS': 'false', 'EMAIL_USE_SSL': 'true'})['EMAIL_PORT'], 465)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class EmailDeliveryWorkflowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user('smtp-owner', 'owner@example.com', 'Original-secret-83!')
        BusinessProfile.objects.create(user=self.user, business_name='SMTP business')

    def test_reset_plain_html_expiry_and_valid_token(self):
        response = self.client.post(reverse('password_reset'), {'email': self.user.email}, secure=True)
        self.assertEqual(response.status_code, 302)
        message = mail.outbox[0]
        self.assertEqual(message.subject, 'Reset your Smart Invoice password')
        self.assertIn('1 hour', message.body)
        self.assertIn('ignore this email', message.body)
        self.assertEqual(message.alternatives[0].mimetype, 'text/html')
        match = re.search(r'https://testserver/accounts/reset/([^/]+)/([^/]+)/', message.body)
        self.assertIsNotNone(match)
        self.assertEqual(int(urlsafe_base64_decode(match[1])), self.user.pk)
        self.assertTrue(default_token_generator.check_token(self.user, match[2]))

    def test_registration_and_resend_share_verification_templates(self):
        response = self.client.post(reverse('register'), {'email': 'new@example.com', 'business_name': 'New business',
            'password1': 'New-secret-83!', 'password2': 'New-secret-83!'})
        self.assertEqual(response.url, reverse('registration_done'))
        user = get_user_model().objects.get(email='new@example.com')
        self.assertFalse(user.is_active)
        first = mail.outbox[0]
        self.assertEqual(first.subject, 'Verify your Smart Invoice email address')
        self.assertIn('24 hours', first.body)
        self.assertIn('Welcome to Smart Invoice', first.body)
        self.assertEqual(first.alternatives[0].mimetype, 'text/html')
        self.client.post(reverse('resend_verification'), {'email': 'new@example.com'})
        self.assertEqual(mail.outbox[1].subject, first.subject)
        link = re.search(r'http://testserver(/\S+)', first.body)[1]
        self.client.post(link)
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_failure_messages_do_not_claim_sent_or_reveal_existence(self):
        for name in ('password_reset', 'resend_verification'):
            if name == 'resend_verification':
                self.user.is_active = False
                self.user.save()
                AccountVerification.objects.create(user=self.user, email=self.user.email)
            with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=smtplib.SMTPAuthenticationError(535, b'private-token-and-password')):
                with self.assertLogs('accounts.emailing', level='WARNING') as logs:
                    response = self.client.post(reverse(name), {'email': self.user.email})
            unknown = self.client.post(reverse(name), {'email': 'unknown@example.com'})
            self.assertEqual(response.url, unknown.url)
            known_page = self.client.get(response.url).content
            unknown_page = self.client.get(unknown.url).content
            self.assertEqual(known_page, unknown_page)
            self.assertNotIn(b"we've sent", known_page)
            self.assertIn(b'temporarily unavailable', known_page)
            self.assertNotIn('private-token-and-password', str(logs.output))
            self.assertIn('authentication_failed', str(logs.output))

    def test_transport_zero_timeout_connection_and_smtp_failures_are_safe(self):
        for failure, category in [(TimeoutError('private'), 'timeout'), (OSError('private'), 'connection_failed'),
                                   (smtplib.SMTPException('private'), 'smtp_rejected')]:
            message = mail.EmailMultiAlternatives('Test', 'private-body', 'from@example.com', ['to@example.com'])
            with patch.object(message, 'send', side_effect=failure), self.assertLogs('accounts.emailing') as logs:
                self.assertFalse(deliver_message(message))
            self.assertIn(category, str(logs.output))
            self.assertNotIn('private', str(logs.output))
        with patch('django.core.mail.EmailMultiAlternatives.send', return_value=0), self.assertLogs('accounts.emailing') as logs:
            self.assertFalse(deliver_message(mail.EmailMultiAlternatives()))
        self.assertIn('not_accepted', str(logs.output))

    def test_rate_limit_prevents_repeated_recovery_delivery(self):
        with override_settings(ACCOUNT_RATE_LIMIT_ATTEMPTS=1):
            self.client.post(reverse('password_reset'), {'email': self.user.email})
            self.client.post(reverse('password_reset'), {'email': self.user.email})
        self.assertEqual(len(mail.outbox), 1)


@override_settings(DEBUG=True, EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend',
    EMAIL_HOST='smtp.gmail.com', EMAIL_PORT=587, EMAIL_USE_TLS=True, EMAIL_USE_SSL=False,
    EMAIL_TIMEOUT=15, EMAIL_HOST_USER='fake@example.test', EMAIL_HOST_PASSWORD='fake-app-password',
    DEFAULT_FROM_EMAIL='fake@example.test')
class TestEmailCommandTests(SimpleTestCase):
    def test_command_uses_active_smtp_starttls_and_sends_one_message(self):
        with patch('smtplib.SMTP') as smtp:
            smtp.return_value.sendmail.return_value = {}
            output = StringIO()
            call_command('test_email_delivery', to='recipient@example.test', stdout=output)
        smtp.assert_called_once_with('smtp.gmail.com', 587, local_hostname=mail.DNS_NAME.get_fqdn(), timeout=15)
        smtp.return_value.starttls.assert_called_once()
        smtp.return_value.login.assert_called_once_with('fake@example.test', 'fake-app-password')
        smtp.return_value.sendmail.assert_called_once()
        self.assertIn('accepted one test message', output.getvalue())
        self.assertIn('not guaranteed', output.getvalue())
        self.assertNotIn('fake-app-password', output.getvalue())

    def test_command_failure_is_sanitized(self):
        output = StringIO()
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=smtplib.SMTPAuthenticationError(535, b'private-secret')), self.assertLogs('accounts.emailing') as logs:
            with self.assertRaises(CommandError) as error:
                call_command('test_email_delivery', to='recipient@example.test', stdout=output)
        self.assertNotIn('private-secret', str(error.exception) + str(logs.output) + output.getvalue())
        self.assertNotIn('accepted one test message', output.getvalue())

    def test_command_refuses_production_non_smtp_invalid_address_and_missing_credentials(self):
        for config, address in [({'DEBUG': False}, 'recipient@example.test'),
                                 ({'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend'}, 'recipient@example.test'),
                                 ({'EMAIL_HOST_PASSWORD': ''}, 'recipient@example.test'), ({}, 'invalid-address')]:
            with self.subTest(config=config), override_settings(**config), patch('django.core.mail.EmailMultiAlternatives.send') as send:
                with self.assertRaises(CommandError):
                    call_command('test_email_delivery', to=address)
                send.assert_not_called()
