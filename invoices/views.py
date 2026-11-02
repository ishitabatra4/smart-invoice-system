
import os
import tempfile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_http_methods, require_POST

from accounts.models import BusinessProfile, BusinessParty

from .models import Invoice, InvoiceLineItem
from .extraction import extract_invoice_data


ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_FILE_SIZE = 10 * 1024 * 1024


def get_or_create_party(business, name, gstin, party_type):
    """
    Find a party by GSTIN within the current business.
    Create a new party if no matching GSTIN exists.
    Never merge parties automatically by name alone.
    """

    name = (name or "").strip()
    gstin = (gstin or "").strip().upper()

    if not name:
        return None

    if gstin:
        party = BusinessParty.objects.filter(
            business=business,
            gstin=gstin,
        ).first()

        if party:
            if party.party_type != party_type:
                if party.party_type == "both":
                    pass
                else:
                    party.party_type = "both"

            if not party.name:
                party.name = name

            party.save()
            return party

    return BusinessParty.objects.create(
        business=business,
        name=name,
        gstin=gstin,
        party_type=party_type,
    )


@login_required
def invoice_list_view(request):
    invoices = Invoice.objects.filter(
        user=request.user
    ).select_related(
        "supplier_party",
        "buyer_party",
    )

    return render(
        request,
        "invoices/invoice_list.html",
        {"invoices": invoices},
    )


@login_required
@require_http_methods(["GET", "POST"])
def invoice_upload_view(request):

    if request.method == "GET":
        return render(request, "invoices/invoice_upload.html")

    uploaded_file = request.FILES.get("source_file")
    invoice_type = request.POST.get("invoice_type", "purchase")

    if invoice_type not in {"sales", "purchase", "expense"}:
        messages.error(request, "Please select a valid invoice type.")
        return render(request, "invoices/invoice_upload.html")

    if not uploaded_file:
        messages.error(request, "Please select a PDF or image file.")
        return render(request, "invoices/invoice_upload.html")

    extension = os.path.splitext(uploaded_file.name.lower())[1]

    if extension not in ALLOWED_EXTENSIONS:
        messages.error(
            request,
            "Only PDF, PNG, JPG and JPEG files are allowed.",
        )
        return render(request, "invoices/invoice_upload.html")

    if uploaded_file.size > MAX_FILE_SIZE:
        messages.error(
            request,
            "File size must not exceed 10 MB.",
        )
        return render(request, "invoices/invoice_upload.html")

    if extension != ".pdf":
        messages.error(
            request,
            "Automatic extraction currently supports text-based PDFs only. "
            "Image and scanned-PDF OCR will be added next.",
        )
        return render(request, "invoices/invoice_upload.html")

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False,
        ) as temp_file:
            for chunk in uploaded_file.chunks():
                temp_file.write(chunk)

            temp_path = temp_file.name

        data = extract_invoice_data(temp_path)

    except Exception:
        messages.error(
            request,
            "Could not read this PDF. Please upload a valid, text-based PDF.",
        )
        return render(request, "invoices/invoice_upload.html")

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

    invoice_number = (
        data["invoice_number"]
        or request.POST.get("invoice_number", "").strip()
    )

    invoice_date = (
        data["invoice_date"]
        or request.POST.get("invoice_date", "").strip()
    )

    supplier_name = (
        data["supplier_name"]
        or request.POST.get("supplier_name", "").strip()
    )

    supplier_gstin = (
        data["supplier_gstin"]
        or request.POST.get("supplier_gstin", "").strip().upper()
    )

    buyer_name = (
        data["buyer_name"]
        or request.POST.get("buyer_name", "").strip()
    )

    buyer_gstin = (
        data["buyer_gstin"]
        or request.POST.get("buyer_gstin", "").strip().upper()
    )

    if not invoice_number or not invoice_date or not supplier_name:
        messages.error(
            request,
            "Required invoice fields could not be extracted. "
            "Please check that the PDF contains readable invoice text.",
        )
        return render(request, "invoices/invoice_upload.html")

    if invoice_type == "sales" and not buyer_name:
        messages.error(
            request,
            "Buyer name could not be extracted. Please provide buyer details.",
        )
        return render(request, "invoices/invoice_upload.html")

    business = BusinessProfile.objects.filter(
        user=request.user
    ).first()

    if not business:
        messages.error(
            request,
            "Please complete your business profile before uploading invoices.",
        )
        return redirect("profile")

    # Duplicate detection:
    # Purchase/expense invoices are identified by supplier GSTIN.
    # Sales invoices are identified by buyer GSTIN.
    if invoice_type in {"purchase", "expense"} and supplier_gstin:
        duplicate_exists = Invoice.objects.filter(
            user=request.user,
            invoice_type=invoice_type,
            supplier_gstin=supplier_gstin,
            invoice_number=invoice_number,
        ).exists()

    elif invoice_type == "sales" and buyer_gstin:
        duplicate_exists = Invoice.objects.filter(
            user=request.user,
            invoice_type="sales",
            buyer_gstin=buyer_gstin,
            invoice_number=invoice_number,
        ).exists()

    else:
        duplicate_exists = Invoice.objects.filter(
            user=request.user,
            invoice_type=invoice_type,
            invoice_number=invoice_number,
        ).exists()

    if duplicate_exists:
        messages.error(
            request,
            "An invoice with this invoice number and party details "
            "already exists.",
        )
        return render(request, "invoices/invoice_upload.html")

    validation_errors = list(data["validation_errors"])

    try:
        with transaction.atomic():

            supplier_party = None
            buyer_party = None

            if invoice_type in {"purchase", "expense"}:
                supplier_party = get_or_create_party(
                    business=business,
                    name=supplier_name,
                    gstin=supplier_gstin,
                    party_type="vendor",
                )

            if invoice_type == "sales":
                buyer_party = get_or_create_party(
                    business=business,
                    name=buyer_name,
                    gstin=buyer_gstin,
                    party_type="customer",
                )

            invoice = Invoice.objects.create(
                user=request.user,
                invoice_type=invoice_type,

                supplier_party=supplier_party,
                buyer_party=buyer_party,

                invoice_number=invoice_number,
                invoice_date=invoice_date,

                supplier_name=supplier_name,
                supplier_gstin=supplier_gstin,

                buyer_name=buyer_name,
                buyer_gstin=buyer_gstin,

                taxable_amount=data["taxable_amount"],
                cgst_amount=data["cgst_amount"],
                sgst_amount=data["sgst_amount"],
                igst_amount=data["igst_amount"],
                other_charges=data["other_charges"],
                total_amount=data["total_amount"],

                extracted_text=data["extracted_text"],
                validation_errors=validation_errors,
                status="review",
                source_file=uploaded_file,
            )

            for item in data["line_items"]:
                InvoiceLineItem.objects.create(
                    invoice=invoice,
                    description=item["description"],
                    hsn_sac_code=item["hsn_sac_code"],
                    quantity=item["quantity"],
                    unit=item.get("unit", ""),
                    rate=item["rate"],
                    tax_rate=item["tax_rate"],
                    taxable_amount=item["taxable_amount"],
                    tax_amount=item["tax_amount"],
                    total_amount=item["total_amount"],
                )

    except (IntegrityError, ValueError):
        messages.error(
            request,
            "The invoice could not be saved. Check the invoice details "
            "and whether this invoice already exists.",
        )
        return render(request, "invoices/invoice_upload.html")

    if validation_errors:
        messages.warning(
            request,
            f"Invoice uploaded, but {len(validation_errors)} "
            "validation issue(s) need review.",
        )
    else:
        messages.success(
            request,
            "Invoice uploaded successfully. Please review it before verification.",
        )

    return redirect("invoice_list")


@login_required
@require_POST
def invoice_delete_view(request, invoice_id):

    invoice = get_object_or_404(
        Invoice,
        pk=invoice_id,
        user=request.user,
    )

    invoice_number = invoice.invoice_number
    file_name = invoice.source_file.name if invoice.source_file else None
    storage = invoice.source_file.storage if invoice.source_file else None

    invoice.delete()

    if file_name and storage:
        try:
            storage.delete(file_name)
        except Exception:
            messages.warning(
                request,
                f"Invoice {invoice_number} was deleted, "
                "but its uploaded file could not be removed.",
            )
            return redirect("invoice_list")

    messages.success(
        request,
        f"Invoice {invoice_number} deleted successfully.",
    )

    return redirect("invoice_list")


@login_required
def invoice_detail_view(request, invoice_id):

    invoice = get_object_or_404(
        Invoice.objects.select_related(
            "supplier_party",
            "buyer_party",
        ).prefetch_related("line_items"),
        pk=invoice_id,
        user=request.user,
    )

    return render(
        request,
        "invoices/invoice_detail.html",
        {"invoice": invoice},
    )
