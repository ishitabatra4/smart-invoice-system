
import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.db import transaction

from .models import BusinessProfile


User = get_user_model()


class RegisterForm(UserCreationForm):
    email = forms.EmailField()
    business_name = forms.CharField(max_length=200)

    gstin = forms.CharField(
        max_length=15,
        required=False,
        label="GSTIN (if registered)",
        widget=forms.TextInput(
            attrs={
                "placeholder": "e.g. 27ABCDE1234F1Z5",
                "maxlength": "15",
                "style": "text-transform: uppercase;",
                "autocomplete": "off",
            }
        ),
    )

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "business_name",
            "gstin",
            "password1",
            "password2",
        ]

    def clean_gstin(self):
        gstin = self.cleaned_data.get("gstin", "").strip().upper()

        # GSTIN is optional for businesses not registered under GST.
        if not gstin:
            return ""

        # Validate the GSTIN's basic structure.
        pattern = (
            r"^[0-9]{2}"
            r"[A-Z]{5}[0-9]{4}[A-Z]"
            r"[1-9A-Z]Z[0-9A-Z]$"
        )

        if not re.fullmatch(pattern, gstin):
            raise forms.ValidationError(
                "Invalid GSTIN format. Please enter a 15-character "
                "GSTIN in the correct format."
            )

        # Validate that the state code is in the recognized range.
        state_code = int(gstin[:2])

        if not (1 <= state_code <= 38 or state_code == 97):
            raise forms.ValidationError(
                "Invalid GSTIN state code."
            )

        # Prevent duplicate GSTIN registration in this application.
        if BusinessProfile.objects.filter(gstin=gstin).exists():
            raise forms.ValidationError(
                "This GSTIN is already registered on this platform."
            )

        return gstin

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=commit)

        if commit:
            BusinessProfile.objects.create(
                user=user,
                business_name=self.cleaned_data["business_name"],
                email=self.cleaned_data["email"],
                gstin=self.cleaned_data.get("gstin") or None,
            )

        return user


class BusinessProfileForm(forms.ModelForm):
    class Meta:
        model = BusinessProfile
        fields = [
            "business_name",
            "legal_name",
            "business_type",
            "gstin",
            "pan",
            "email",
            "phone",
            "address_line",
            "city",
            "state",
            "pincode",
        ]

        widgets = {
            "business_name": forms.TextInput(attrs={"placeholder": "Business name"}),
            "legal_name": forms.TextInput(attrs={"placeholder": "Legal name"}),
            "gstin": forms.TextInput(attrs={
                "placeholder": "15-character GSTIN",
                "maxlength": "15",
                "style": "text-transform: uppercase;",
                "autocomplete": "off",
            }),
            "pan": forms.TextInput(attrs={
                "placeholder": "10-character PAN",
                "maxlength": "10",
                "style": "text-transform: uppercase;",
            }),
            "email": forms.EmailInput(attrs={"placeholder": "Business email"}),
            "phone": forms.TextInput(attrs={"placeholder": "Contact number"}),
            "address_line": forms.Textarea(attrs={"rows": 3, "placeholder": "Business address"}),
            "city": forms.TextInput(attrs={"placeholder": "City"}),
            "state": forms.TextInput(attrs={"placeholder": "State"}),
            "pincode": forms.TextInput(attrs={"placeholder": "6-digit PIN code", "maxlength": "6"}),
        }

    def clean_gstin(self):
        gstin = (self.cleaned_data.get("gstin") or "").strip().upper()

        if not gstin:
            return None

        import re

        pattern = (
            r"^[0-9]{2}"
            r"[A-Z]{5}[0-9]{4}[A-Z]"
            r"[1-9A-Z]Z[0-9A-Z]$"
        )

        if not re.fullmatch(pattern, gstin):
            raise forms.ValidationError("Enter a GSTIN in the correct 15-character format.")

        state_code = int(gstin[:2])
        if not (1 <= state_code <= 38 or state_code == 97):
            raise forms.ValidationError("Invalid GSTIN state code.")

        duplicate = BusinessProfile.objects.filter(gstin=gstin).exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise forms.ValidationError("This GSTIN is already associated with another business on this platform.")

        return gstin

    def clean_pan(self):
        pan = (self.cleaned_data.get("pan") or "").strip().upper()

        if pan and not forms.re.fullmatch(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan):
            raise forms.ValidationError("Enter a PAN in the correct 10-character format.")

        return pan

    def clean_pincode(self):
        pincode = (self.cleaned_data.get("pincode") or "").strip()

        if pincode and not pincode.isdigit():
            raise forms.ValidationError("PIN code must contain digits only.")

        if pincode and len(pincode) != 6:
            raise forms.ValidationError("PIN code must contain exactly 6 digits.")

        return pincode

