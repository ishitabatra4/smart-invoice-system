
import re
import uuid

from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.db import transaction

from .models import AccountVerification, BusinessParty, BusinessProfile
from .backends import email_candidates, normalize_email


User = get_user_model()


class RegisterForm(UserCreationForm):
    email = forms.EmailField(label="Email address", max_length=254)
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
            "email",
            "business_name",
            "gstin",
            "password1",
            "password2",
        ]

    def clean_email(self):
        email = normalize_email(self.cleaned_data["email"])
        if email_candidates(email).exists() or AccountVerification.objects.filter(email=email).exists():
            raise forms.ValidationError("This email is already registered. Log in or recover your account.")
        return email

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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].widget.attrs.update({"autocomplete": "email", "placeholder": "you@company.com", "autofocus": True})
        self.fields["business_name"].widget.attrs.update({"autocomplete": "organization", "placeholder": "Your business name"})
        self.fields["password1"].widget.attrs["autocomplete"] = "new-password"
        self.fields["password2"].widget.attrs["autocomplete"] = "new-password"
        self.fields["password2"].label = "Confirm password"

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=False)
        if not user.username:
            user.username = "account_" + uuid.uuid4().hex

        if commit:
            user.is_active = False
            user.save()
            AccountVerification.objects.create(user=user, email=self.cleaned_data["email"])
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

        if pan and not re.fullmatch(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan):
            raise forms.ValidationError("Enter a PAN in the correct 10-character format.")

        return pan

    def clean_pincode(self):
        pincode = (self.cleaned_data.get("pincode") or "").strip()

        if pincode and not pincode.isdigit():
            raise forms.ValidationError("PIN code must contain digits only.")

        if pincode and len(pincode) != 6:
            raise forms.ValidationError("PIN code must contain exactly 6 digits.")

        return pincode


class BusinessPartyForm(forms.ModelForm):
    party_type = forms.ChoiceField(choices=(
        ("customer", "Customer"),
        ("vendor", "Supplier"),
        ("both", "Both"),
    ))

    class Meta:
        model = BusinessParty
        fields = [
            "name", "legal_name", "party_type", "gstin", "email", "phone",
            "address_line", "state", "city", "pincode",
        ]
        labels = {"name": "Party Name", "address_line": "Address"}
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Party name"}),
            "legal_name": forms.TextInput(attrs={"placeholder": "Registered legal name"}),
            "gstin": forms.TextInput(attrs={"placeholder": "15-character GSTIN", "maxlength": 15, "autocomplete": "off"}),
            "email": forms.EmailInput(attrs={"placeholder": "name@example.com"}),
            "phone": forms.TextInput(attrs={"placeholder": "+91 9876543210", "inputmode": "tel"}),
            "address_line": forms.Textarea(attrs={"rows": 3, "placeholder": "Street address"}),
            "state": forms.TextInput(attrs={"placeholder": "State"}),
            "city": forms.TextInput(attrs={"placeholder": "City"}),
            "pincode": forms.TextInput(attrs={"placeholder": "6-digit PIN code", "maxlength": 6, "inputmode": "numeric"}),
        }

    def __init__(self, *args, business, **kwargs):
        self.business = business
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if field == self.fields["party_type"] else "form-control"
        self.fields["name"].required = True
        self.fields["party_type"].initial = self.instance.party_type or "vendor"

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if not name:
            raise forms.ValidationError("Enter a party name.")
        return name

    def clean_gstin(self):
        gstin = re.sub(r"\s+", "", self.cleaned_data.get("gstin") or "").upper()
        if not gstin:
            return ""
        pattern = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
        if not re.fullmatch(pattern, gstin):
            raise forms.ValidationError("Enter a valid 15-character GSTIN.")
        state_code = int(gstin[:2])
        if not (1 <= state_code <= 38 or state_code == 97):
            raise forms.ValidationError("GSTIN contains an invalid state code.")
        duplicates = BusinessParty.objects.filter(business=self.business).exclude(pk=self.instance.pk)
        if any(re.sub(r"\s+", "", value or "").upper() == gstin for value in duplicates.values_list("gstin", flat=True)):
            raise forms.ValidationError("A party with this GSTIN already exists in your business.")
        return gstin

    def clean_phone(self):
        raw = (self.cleaned_data.get("phone") or "").strip()
        if not raw:
            return ""
        compact = re.sub(r"[\s().-]", "", raw)
        if compact.startswith("+91"):
            compact = compact[3:]
        elif compact.startswith("0091"):
            compact = compact[4:]
        elif compact.startswith("0") and len(compact) == 11:
            compact = compact[1:]
        if not re.fullmatch(r"[6-9][0-9]{9}", compact):
            raise forms.ValidationError("Enter a valid 10-digit Indian mobile number, optionally with the +91 country code.")
        return compact

    def clean_pincode(self):
        pincode = (self.cleaned_data.get("pincode") or "").strip()
        if pincode and not re.fullmatch(r"[1-9][0-9]{5}", pincode):
            raise forms.ValidationError("Enter a valid 6-digit Indian PIN code.")
        return pincode



class EmailLoginForm(forms.Form):
    email = forms.EmailField(label="Email address", max_length=254,
                             widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "you@company.com", "autofocus": True}))
    password = forms.CharField(label="Password", strip=False,
                               widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "placeholder": "Enter your password"}))

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        self.unverified = False
        super().__init__(*args, **kwargs)

    def clean_email(self):
        return normalize_email(self.cleaned_data["email"])

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("email") and cleaned.get("password"):
            self.user_cache = authenticate(self.request, email=cleaned["email"], password=cleaned["password"])
            if self.user_cache is None:
                self.unverified = bool(getattr(self.request, "email_unverified", False))
                if not self.unverified:
                    raise forms.ValidationError("The email address or password is incorrect. Please try again.")
                raise forms.ValidationError("Your email address hasn't been verified yet.")
        return cleaned

    def get_user(self):
        return self.user_cache


class ResendVerificationForm(forms.Form):
    email = forms.EmailField(label="Email address", max_length=254,
                             widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "you@company.com", "autofocus": True}))

    def clean_email(self):
        return normalize_email(self.cleaned_data["email"])
