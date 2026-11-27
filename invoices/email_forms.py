from django import forms
from django.core import signing
from django.core.exceptions import ValidationError

from .email_delivery import recipient_address

TOKEN_SALT = "invoices.email.confirmation"


class InvoiceEmailForm(forms.Form):
    send_token = forms.CharField(widget=forms.HiddenInput)
    recipient = forms.EmailField(max_length=254, label="Recipient email", widget=forms.EmailInput(attrs={"class": "form-control", "autocomplete": "email"}))
    confirm = forms.BooleanField(label="I have verified the recipient and want to send this invoice PDF.")
    confirm_override = forms.BooleanField(required=False, label="I explicitly approve the corrected recipient for this attempt only.")
    confirm_resend = forms.BooleanField(required=False, label="I checked the uncertain attempt and deliberately want a new send. I understand duplicates are possible.")

    def __init__(self, *args, user, invoice, retry=None, **kwargs):
        self.user, self.invoice, self.retry = user, invoice, retry
        super().__init__(*args, **kwargs)

    def clean_recipient(self):
        # Check raw text too: EmailField normally strips leading/trailing newlines.
        return recipient_address(self.data.get("recipient", ""))

    def clean_send_token(self):
        try:
            value = signing.loads(self.cleaned_data["send_token"], salt=TOKEN_SALT, max_age=3600)
            if (value["user"], value["invoice"], value["retry"]) != (self.user.pk, self.invoice.pk, self.retry.pk if self.retry else None):
                raise ValueError
            return value["key"]
        except (signing.BadSignature, KeyError, ValueError, TypeError):
            raise ValidationError("This confirmation expired or is invalid. Reload the form.") from None
