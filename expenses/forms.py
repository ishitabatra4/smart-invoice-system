import re
from decimal import Decimal, ROUND_HALF_UP
from PIL import Image, UnidentifiedImageError

from django import forms
from django.conf import settings
from invoices.manual_calculations import calculate_invoice
from django.core.exceptions import ValidationError
from django.db.models import Q

from accounts.models import BusinessParty
from invoices.models import Invoice, InvoicePayment

from .models import ExpenseCategory, RecurringExpense


CENT = Decimal("0.01")
GST_RATES = (("0", "0%"), ("5", "5%"), ("12", "12%"), ("18", "18%"), ("28", "28%"))


class ExpenseForm(forms.Form):
    title = forms.CharField(max_length=200, label="Expense title")
    category = forms.ModelChoiceField(queryset=ExpenseCategory.objects.none())
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    expense_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    due_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    reference_number = forms.CharField(max_length=100, label="Reference / bill number")
    supplier_party = forms.ModelChoiceField(queryset=BusinessParty.objects.none(), required=False, label="Existing supplier")
    add_new_supplier = forms.BooleanField(required=False, label="Add a new supplier")
    supplier_name = forms.CharField(max_length=200, required=False)
    supplier_gstin = forms.CharField(max_length=15, required=False, label="Supplier GSTIN")
    supplier_email = forms.EmailField(required=False, label="Supplier email")
    supplier_phone = forms.CharField(max_length=15, required=False, label="Supplier phone")
    amount = forms.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01"), label="Amount",
        help_text="Enter the bill amount before GST, or the GST-inclusive amount as selected below.",
    )
    gst_rate = forms.ChoiceField(choices=GST_RATES, initial="0", label="GST rate")
    gst_inclusive = forms.ChoiceField(choices=(("exclusive", "GST exclusive"), ("inclusive", "GST inclusive")), initial="exclusive", label="Amount includes GST?")
    discount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, initial=Decimal("0.00"))
    supplier_state_code = forms.CharField(max_length=2, label="Supplier state code")
    place_of_supply = forms.CharField(max_length=2, label="Place of supply state code")
    supply_category = forms.ChoiceField(choices=[("","Select supply category"),("standard","Standard domestic supply"),("special","Special supply — manual review required")],initial="standard")
    tax_treatment = forms.ChoiceField(choices=[("","Select tax treatment"),("taxable","Taxable, including 0%"),("exempt","Exempt"),("nil_rated","Nil-rated"),("non_gst","Non-GST")],initial="taxable")
    payment_status = forms.ChoiceField(choices=(("unpaid", "Unpaid"), ("partial", "Partially paid"), ("paid", "Paid")), initial="unpaid")
    amount_paid = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, initial=Decimal("0.00"))
    payment_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    payment_mode = forms.ChoiceField(choices=InvoicePayment.PAYMENT_MODES, required=False)
    payment_reference = forms.CharField(max_length=100, required=False, label="Payment transaction/reference ID")
    attachment = forms.FileField(required=False, label="Bill or receipt (PDF, JPG, JPEG, PNG)")
    remove_attachment = forms.BooleanField(required=False, label="Remove current attachment")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    acknowledge_duplicates = forms.BooleanField(required=False,label="I reviewed possible same-business duplicates; this is a distinct expense.")

    def __init__(self, *args, business, invoice=None, detail=None, allow_initial_payment=True, **kwargs):
        self.business = business
        self.invoice = invoice
        self.detail = detail
        self.allow_initial_payment = allow_initial_payment
        super().__init__(*args, **kwargs)
        self.fields["gst_rate"].choices=[("","Select a reviewed GST rate")]+[(value,value+"%") for value in settings.GST_ALLOWED_RATES]
        category_filter = Q(business=business, is_active=True)
        if detail and detail.category_id:
            category_filter |= Q(pk=detail.category_id, business=business)
        self.fields["category"].queryset = ExpenseCategory.objects.filter(category_filter)
        self.fields["supplier_party"].queryset = BusinessParty.objects.filter(
            business=business, is_active=True, party_type__in=("vendor", "both")
        )
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-select" if name in {"category", "supplier_party", "gst_rate", "gst_inclusive", "payment_status", "payment_mode"} else "form-control"
        if not allow_initial_payment:
            for name in ("payment_status", "amount_paid", "payment_date", "payment_mode", "payment_reference"):
                self.fields.pop(name)
        if invoice and detail and not self.is_bound:
            inclusive = invoice.pricing_mode == "inclusive" if invoice.calculation_version == 1 and invoice.pricing_mode in {"inclusive", "exclusive"} else detail.gst_inclusive
            existing_rate = invoice.line_items.first().tax_rate if invoice.line_items.exists() else Decimal("0")
            rate_value = format(existing_rate.normalize(), "f")
            if "." in rate_value:
                rate_value = rate_value.rstrip("0").rstrip(".")
            self.initial.update({
                "supplier_state_code": invoice.supplier_state_code, "place_of_supply":invoice.place_of_supply,
                "supply_category":invoice.supply_category or "", "tax_treatment":invoice.line_items.first().tax_treatment if invoice.calculation_version==1 and invoice.line_items.exists() else "",
                "title": detail.title,
                "category": detail.category_id,
                "description": detail.description,
                "expense_date": invoice.invoice_date,
                "due_date": invoice.due_date,
                "reference_number": detail.reference_number or invoice.invoice_number,
                "supplier_party": invoice.supplier_party_id,
                "supplier_name": invoice.supplier_name,
                "supplier_gstin": invoice.supplier_gstin,
                "amount": invoice.total_amount + detail.discount if inclusive else invoice.taxable_amount + detail.discount,
                "gst_rate": rate_value or "0",
                "gst_inclusive": "inclusive" if inclusive else "exclusive",
                "discount": detail.discount,
                "notes": detail.notes,
            })

    def clean_supplier_gstin(self):
        gstin = re.sub(r"\s+", "", self.cleaned_data.get("supplier_gstin") or "").upper()
        if gstin and not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", gstin):
            raise forms.ValidationError("Enter a valid 15-character supplier GSTIN.")
        if gstin and int(gstin[:2]) not in (*range(1, 39), 97):
            raise forms.ValidationError("Supplier GSTIN has an invalid state code.")
        return gstin

    def clean_supplier_phone(self):
        value = (self.cleaned_data.get("supplier_phone") or "").strip()
        if not value:
            return ""
        digits = re.sub(r"[\s().-]", "", value)
        if digits.startswith("+91"):
            digits = digits[3:]
        elif digits.startswith("0") and len(digits) == 11:
            digits = digits[1:]
        if not re.fullmatch(r"[6-9][0-9]{9}", digits):
            raise forms.ValidationError("Enter a valid Indian mobile number.")
        return digits

    def clean(self):
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        new_supplier = cleaned.get("add_new_supplier")
        if new_supplier:
            if not cleaned.get("supplier_name", "").strip():
                self.add_error("supplier_name", "Enter the new supplier name.")
            gstin = cleaned.get("supplier_gstin")
            if gstin and BusinessParty.objects.filter(business=self.business, gstin__iexact=gstin).exists():
                self.add_error("supplier_gstin", "A supplier with this GSTIN already exists. Select that supplier instead.")
        elif not cleaned.get("supplier_party") and not (self.invoice and self.invoice.supplier_name):
            self.add_error("supplier_party", "Select an existing supplier or choose Add a new supplier.")
        if new_supplier:
            cleaned["supplier_party"] = None

        date_value, due_date = cleaned.get("expense_date"), cleaned.get("due_date")
        if date_value and due_date and due_date < date_value:
            self.add_error("due_date", "Due date cannot be earlier than the expense date.")
        amount, discount = cleaned.get("amount"), cleaned.get("discount") or Decimal("0.00")
        needed=("supplier_state_code","place_of_supply","supply_category","tax_treatment","gst_rate")
        if amount is not None and all(cleaned.get(name) for name in needed):
            try:
                result=calculate_invoice([{"quantity":Decimal("1"),"rate":amount,"discount":discount,"tax_rate":Decimal(cleaned["gst_rate"]),"tax_treatment":cleaned["tax_treatment"]}],
                    cleaned["supplier_state_code"],cleaned["place_of_supply"],pricing_mode=cleaned.get("gst_inclusive"),
                    supply_category=cleaned["supply_category"],allowed_rates=settings.GST_ALLOWED_RATES)
                cleaned.update(calculated_taxable=result["taxable_amount"],calculated_tax=result["tax_amount"],calculated_total=result["total_amount"],gst_result=result)
            except ValidationError as error:
                self.add_error(None,error)
        supplier=cleaned.get("supplier_party")
        gstin=cleaned.get("supplier_gstin") if new_supplier else (supplier.gstin if supplier else (self.invoice.supplier_gstin if self.invoice else ""))
        if gstin and cleaned.get("supplier_state_code") and gstin[:2]!=cleaned["supplier_state_code"]:
            self.add_error("supplier_state_code","Supplier state must match its GSTIN.")

        if self.allow_initial_payment:
            status = cleaned.get("payment_status")
            paid = cleaned.get("amount_paid") or Decimal("0.00")
            total = cleaned.get("calculated_total")
            if total is not None:
                if paid > total:
                    self.add_error("amount_paid", "Payment cannot exceed the expense total.")
                elif status == "paid" and paid != total:
                    self.add_error("amount_paid", "For Paid status, amount paid must equal the expense total.")
                elif status == "partial" and not (Decimal("0.00") < paid < total):
                    self.add_error("amount_paid", "Enter an amount greater than zero and less than the total for a partial payment.")
                elif status == "unpaid" and paid:
                    self.add_error("amount_paid", "Unpaid expenses must have an amount paid of zero.")
            if paid:
                if not cleaned.get("payment_date"):
                    self.add_error("payment_date", "Enter the payment date.")
                elif date_value and cleaned["payment_date"] < date_value:
                    self.add_error("payment_date", "Payment date cannot be earlier than the expense date.")
                if not cleaned.get("payment_mode"):
                    self.add_error("payment_mode", "Select the payment mode.")
                if not cleaned.get("payment_reference", "").strip():
                    self.add_error("payment_reference", "Enter a payment reference to prevent duplicate payments.")

        attachment = cleaned.get("attachment")
        if attachment:
            from invoices.file_security import validate_document
            try:
                validate_document(attachment)
            except ValidationError as exc:
                self.add_error("attachment", exc)
        return cleaned


class RecurringExpenseForm(forms.ModelForm):
    gst_rate = forms.ChoiceField(choices=GST_RATES, label="GST rate")

    class Meta:
        model = RecurringExpense
        fields = ["title", "category", "supplier", "taxable_amount", "gst_rate", "gst_inclusive", "frequency", "start_date", "end_date", "supplier_state_code", "place_of_supply", "supply_category", "tax_treatment"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "gst_rate": forms.Select(choices=GST_RATES),
        }

    def __init__(self, *args, business, **kwargs):
        self.business = business
        super().__init__(*args, **kwargs)
        self.fields["gst_rate"].choices=[("","Select a reviewed GST rate")]+[(value,value+"%") for value in settings.GST_ALLOWED_RATES]
        if self.instance.pk:
            self.initial["gst_rate"]=format(self.instance.gst_rate.normalize(),"f")
        for name in ("supplier_state_code","place_of_supply","supply_category","tax_treatment"):
            self.fields[name].required=True
        self.fields["supply_category"]=forms.ChoiceField(choices=[("","Select supply category"),("standard","Standard domestic supply"),("special","Special supply — manual review required")],initial="standard")
        self.fields["tax_treatment"]=forms.ChoiceField(choices=[("","Select tax treatment"),("taxable","Taxable, including 0%"),("exempt","Exempt"),("nil_rated","Nil-rated"),("non_gst","Non-GST")],initial="taxable")
        self.fields["category"].queryset = ExpenseCategory.objects.filter(business=business, is_active=True)
        self.fields["supplier"].queryset = BusinessParty.objects.filter(business=business, is_active=True, party_type__in=("vendor", "both"))
        self.fields["supplier"].required = False
        self.fields["taxable_amount"].label = "Amount"
        self.fields["taxable_amount"].help_text = "Amount before GST, or the GST-inclusive amount when selected below."
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-select" if name in {"category", "supplier", "frequency", "gst_rate", "gst_inclusive"} else "form-control"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("start_date"):
            cleaned["next_due_date"] = cleaned["start_date"]
            self.instance.start_date = cleaned["start_date"]
            self.instance.next_due_date = cleaned["start_date"]
        self.instance.business = self.business
        if all(cleaned.get(name) is not None for name in ("taxable_amount","gst_rate","supplier_state_code","place_of_supply","supply_category","tax_treatment")):
            try:
                calculate_invoice([{"quantity":Decimal("1"),"rate":cleaned["taxable_amount"],"tax_rate":Decimal(cleaned["gst_rate"]),"tax_treatment":cleaned["tax_treatment"]}],
                    cleaned["supplier_state_code"],cleaned["place_of_supply"],pricing_mode="inclusive" if cleaned.get("gst_inclusive") else "exclusive",supply_category=cleaned["supply_category"],allowed_rates=settings.GST_ALLOWED_RATES)
            except ValidationError as error:
                self.add_error(None,error)
        if cleaned.get("supplier") and cleaned["supplier"].gstin and cleaned.get("supplier_state_code") != cleaned["supplier"].gstin[:2]:
            self.add_error("supplier_state_code","Supplier state must match its GSTIN.")
        if cleaned.get("gst_rate"):
            self.instance.gst_rate = Decimal(cleaned["gst_rate"])
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.business = self.business
        instance.gst_rate = Decimal(self.cleaned_data["gst_rate"])
        if instance.pk:
            previous_next_due = RecurringExpense.objects.get(pk=instance.pk, business=self.business).next_due_date
            instance.next_due_date = max(previous_next_due, self.cleaned_data["start_date"])
        else:
            instance.next_due_date = self.cleaned_data["next_due_date"]
        instance.schedule_day = self.cleaned_data["start_date"].day
        if commit:
            instance.full_clean()
            instance.save()
        return instance


class ExpenseReportFilterForm(forms.Form):
    REPORT_TYPES = (
        ("complete", "Complete expense report"), ("monthly", "Monthly expense report"),
        ("financial_year", "Financial-year expense report"), ("category", "Category-wise report"),
        ("supplier", "Supplier-wise report"), ("payment_status", "Payment status report"),
        ("gst", "GST expense summary"), ("itc", "ITC preparation summary"), ("linked", "Linked purchases"), ("recurring", "Recurring occurrences"), ("payments", "Expense payment history"),
    )
    itc_status = forms.ChoiceField(choices=[("", "All ITC statuses"),("pending","Pending"),("potential","Potential"),("confirmed","Confirmed"),("ineligible","Ineligible"),("reversed","Reversed")],required=False)
    report_type = forms.ChoiceField(choices=REPORT_TYPES, required=False, initial="complete")
    start_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    category = forms.ModelChoiceField(queryset=ExpenseCategory.objects.none(), required=False)
    supplier = forms.ModelChoiceField(queryset=BusinessParty.objects.none(), required=False)
    status = forms.ChoiceField(choices=(("", "All statuses"), ("paid", "Paid"), ("partial", "Partially paid"), ("unpaid", "Unpaid"), ("overdue", "Overdue")), required=False)

    def __init__(self, *args, business, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = ExpenseCategory.objects.filter(business=business)
        self.fields["supplier"].queryset = BusinessParty.objects.filter(business=business, party_type__in=("vendor", "both"))
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field, (forms.ChoiceField, forms.ModelChoiceField)) else "form-control"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("start_date") and cleaned.get("end_date") and cleaned["start_date"] > cleaned["end_date"]:
            raise forms.ValidationError("Start date must be on or before end date.")
        return cleaned


class ExpenseMetadataForm(forms.Form):
    title=forms.CharField(max_length=200)
    category=forms.ModelChoiceField(queryset=ExpenseCategory.objects.none())
    description=forms.CharField(required=False,widget=forms.Textarea(attrs={'rows':3}))
    notes=forms.CharField(required=False,widget=forms.Textarea(attrs={'rows':3}))
    attachment=forms.FileField(required=False)
    remove_attachment=forms.BooleanField(required=False)
    def __init__(self,*args,business,invoice,detail=None,**kwargs):
        super().__init__(*args,**kwargs)
        if detail:
            original=ExpenseForm(business=business,invoice=invoice,detail=detail,allow_initial_payment=False)
            self.initial.update(original.initial)
        else:self.initial.update(title=invoice.supplier_name or invoice.invoice_number)
        self.fields['category'].queryset=ExpenseCategory.objects.filter(business=business).filter(Q(is_active=True)|Q(pk=detail.category_id if detail else None))
        for field in self.fields.values():field.widget.attrs['class']='form-control'
    def clean_attachment(self):
        attachment=self.cleaned_data.get('attachment')
        if attachment:
            from invoices.file_security import validate_document
            validate_document(attachment)
        return attachment


class ITCReviewForm(forms.Form):
    from .models import ExpenseITCReview
    status=forms.ChoiceField(choices=ExpenseITCReview.STATUSES)
    reason=forms.CharField(max_length=2000,widget=forms.Textarea(attrs={'rows':3}))
    attested=forms.BooleanField(required=False,label='I independently reviewed registration, tax documentation, receipt, business use, blocked credits and applicable eligibility conditions. This does not file or claim ITC.')


class PurchaseExpenseForm(forms.Form):
    invoice=forms.ModelChoiceField(queryset=Invoice.objects.none(),label='Existing verified purchase')
    category=forms.ModelChoiceField(queryset=ExpenseCategory.objects.none())
    confirm=forms.BooleanField(label='Link expense metadata to this same purchase; create no new financial record.')
    def __init__(self,*args,business,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['invoice'].queryset=Invoice.objects.filter(user=business.user,invoice_type='purchase',status__in=['verified','paid'])
        self.fields['category'].queryset=ExpenseCategory.objects.filter(business=business,is_active=True)
