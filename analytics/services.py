import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Count, Prefetch, Sum
from django.core.exceptions import ValidationError
from config.report_limits import validate_report_range, MAX_MONTH_BUCKETS
from django.utils import timezone

from invoices.models import Invoice, InvoicePayment
from invoices.ledger import invoice_outstanding
from taxation.models import ReconciliationRun


ZERO = Decimal("0.00")
CONFIRMED_STATUSES = ("verified", "paid")
FINANCIAL_STATUSES = ("uploaded", "processing", "review", "verified", "paid")


def _first_of_month(value):
    return value.replace(day=1)


def _shift_month(value, amount):
    month_index = value.year * 12 + value.month - 1 + amount
    year, month_index = divmod(month_index, 12)
    return date(year, month_index + 1, 1)


def dashboard_date_range(params, today=None):
    today = today or timezone.localdate()
    preset = "custom" if params.get("dates_edited") == "1" else params.get("period", "this_month")
    current_month = _first_of_month(today)
    if preset == "last_month":
        start = _shift_month(current_month, -1)
        end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    elif preset == "last_3_months":
        start, end = _shift_month(current_month, -2), today
    elif preset == "last_6_months":
        start, end = _shift_month(current_month, -5), today
    elif preset == "financial_year":
        fy_year = today.year if today.month >= 4 else today.year - 1
        start, end = date(fy_year, 4, 1), today
    elif preset == "custom":
        try:
            start = date.fromisoformat(params.get("start_date", ""))
            end = date.fromisoformat(params.get("end_date", ""))
            validate_report_range(start, end)
            return start, end, preset, None
        except (TypeError, ValueError, ValidationError):
            return current_month, today, "this_month", "Enter a valid custom date range. Showing this month instead."
    else:
        start, end, preset = current_month, today, "this_month"
    return start, end, preset, None


def _month_labels(start, end):
    count = (end.year - start.year) * 12 + end.month - start.month + 1
    if not 1 <= count <= MAX_MONTH_BUCKETS:
        raise ValidationError(f'Report supports at most {MAX_MONTH_BUCKETS} monthly buckets.')
    month = _first_of_month(start)
    final_month = _first_of_month(end)
    labels, keys = [], []
    while month <= final_month:
        labels.append(month.strftime("%b %Y"))
        keys.append(month.strftime("%Y-%m"))
        if month == final_month:
            break
        month = _shift_month(month, 1)
    return labels, keys


def build_analytics_context(user, start, end, preset, custom_error=None):
    confirmed_invoices = list(Invoice.objects.filter(
        user=user,
        status__in=CONFIRMED_STATUSES,
        invoice_type__in=("sales", "purchase", "expense"),
        invoice_date__range=(start, end),
    ).select_related("buyer_party__business", "supplier_party__business").prefetch_related(
        Prefetch("payments", queryset=InvoicePayment.objects.filter(user=user))
    ))
    from invoices.adjustment_services import adjustments_available
    if adjustments_available():
        from django.db.models import prefetch_related_objects
        prefetch_related_objects(confirmed_invoices,'adjustment_notes')
    else:
        for invoice in confirmed_invoices:invoice._prefetched_objects_cache['adjustment_notes']=[]
    status_counts = defaultdict(int)
    from invoices.date_evidence import with_invoice_dates
    for status, count in with_invoice_dates(Invoice.objects.filter(
        user=user,
        status__in=FINANCIAL_STATUSES,
    )).filter(evidence_invoice_date__range=(start, end)).values_list("status").annotate(total_count=Count("id")):
        status_counts[status] = count

    payments = list(InvoicePayment.objects.filter(
        user=user, invoice__user=user, party__business__user=user,
        payment_date__range=(start, end),
        invoice__status__in=CONFIRMED_STATUSES,
        invoice__invoice_type__in=("sales", "purchase", "expense"),
    ).select_related("invoice", "party"))
    labels, month_keys = _month_labels(start, end)
    series = {
        key: {month: ZERO for month in month_keys}
        for key in ("sales", "purchases", "expenses", "cash_in", "cash_out",
                    "taxable_sales", "taxable_purchases", "output_gst", "input_gst",
                    "output_cgst", "output_sgst", "output_igst",
                    "input_cgst", "input_sgst", "input_igst")
    }
    totals = {
        "sales": ZERO, "purchases": ZERO, "expenses": ZERO,
        "taxable_sales": ZERO, "taxable_purchases": ZERO,
        "payments_received": ZERO, "payments_made": ZERO,
        "receivable": ZERO, "payable": ZERO,
        "output_gst": ZERO, "input_gst": ZERO,
        "output_cgst": ZERO, "output_sgst": ZERO, "output_igst": ZERO,
        "input_cgst": ZERO, "input_sgst": ZERO, "input_igst": ZERO,
    }
    customer_sales = defaultdict(lambda: ZERO)
    supplier_purchases = defaultdict(lambda: ZERO)

    treatment_summary={kind:{name:ZERO for name in ("taxable","exempt","nil_rated","non_gst","unclassified")} for kind in ("sales","purchase","expense")}
    for invoice in confirmed_invoices:
        if invoice.calculation_version==1 and invoice.treatment_breakdown:
            for name,value in invoice.treatment_breakdown.items():
                if name in treatment_summary[invoice.invoice_type]:
                    treatment_summary[invoice.invoice_type][name]+=Decimal(value)
        else:
            treatment_summary[invoice.invoice_type]["unclassified"]+=invoice.taxable_amount
        month_key = invoice.invoice_date.strftime("%Y-%m")
        if invoice.invoice_type == "sales":
            totals["sales"] += invoice.total_amount
            totals["taxable_sales"] += invoice.taxable_amount
            series["sales"][month_key] += invoice.total_amount
            series["taxable_sales"][month_key] += invoice.taxable_amount
            series["output_gst"][month_key] += invoice.total_tax
            totals["output_gst"] += invoice.total_tax
            for tax_name in ("cgst", "sgst", "igst"):
                value = getattr(invoice, f"{tax_name}_amount")
                totals[f"output_{tax_name}"] += value
                series[f"output_{tax_name}"][month_key] += value
            customer_name = invoice.buyer_party.name if invoice.buyer_party_id and invoice.buyer_party.business.user_id == user.pk else invoice.buyer_name or "Unknown customer"
            customer_sales[customer_name] += invoice.total_amount
            outstanding = invoice_outstanding(invoice, as_of=end)
            totals["receivable"] += outstanding
        elif invoice.invoice_type == "purchase":
            totals["purchases"] += invoice.total_amount
            totals["taxable_purchases"] += invoice.taxable_amount
            series["purchases"][month_key] += invoice.total_amount
            series["taxable_purchases"][month_key] += invoice.taxable_amount
            series["input_gst"][month_key] += invoice.total_tax
            totals["input_gst"] += invoice.total_tax
            for tax_name in ("cgst", "sgst", "igst"):
                value = getattr(invoice, f"{tax_name}_amount")
                totals[f"input_{tax_name}"] += value
                series[f"input_{tax_name}"][month_key] += value
            supplier_name = invoice.supplier_party.name if invoice.supplier_party_id and invoice.supplier_party.business.user_id == user.pk else invoice.supplier_name or "Unknown supplier"
            supplier_purchases[supplier_name] += invoice.total_amount
            outstanding = invoice_outstanding(invoice, as_of=end)
            totals["payable"] += outstanding
        elif invoice.invoice_type == "expense":
            totals["expenses"] += invoice.total_amount
            series["expenses"][month_key] += invoice.total_amount
            totals["payable"] += invoice_outstanding(invoice, as_of=end)

    from invoices.adjustment_reporting import note_events
    adjustment_events = note_events(user, start, end)
    seen_balance_ids={invoice.pk for invoice in confirmed_invoices}
    for event in adjustment_events:
        invoice=event['note'].original_invoice
        if invoice.pk not in seen_balance_ids:
            seen_balance_ids.add(invoice.pk)
            totals['receivable' if invoice.invoice_type=='sales' else 'payable'] += invoice_outstanding(invoice,as_of=end)
    gross_original = {key: totals[key] for key in ("sales", "purchases", "taxable_sales", "taxable_purchases", "output_gst", "input_gst")}
    for event in adjustment_events:
        note = event["note"]
        sales = event["invoice_type"] == "sales"
        side, taxside = ("sales", "output") if sales else ("purchases", "input")
        taxable_key = "taxable_sales" if sales else "taxable_purchases"
        key = event["date"].strftime("%Y-%m")
        totals[side] += event["total_amount"]
        totals[taxable_key] += event["taxable_amount"]
        series[side][key] += event["total_amount"]
        series[taxable_key][key] += event["taxable_amount"]
        tax = sum((event[k+"_amount"] for k in ("cgst","sgst","igst")), ZERO)
        totals[taxside+"_gst"] += tax
        series[taxside+"_gst"][key] += tax
        for component in ("cgst","sgst","igst"):
            totals[taxside+"_"+component] += event[component+"_amount"]
            series[taxside+"_"+component][key] += event[component+"_amount"]
        for treatment, amount in note.treatment_breakdown.items():
            treatment_summary[event["invoice_type"]][treatment] += event["sign"] * Decimal(amount)
        directory = customer_sales if sales else supplier_purchases
        directory[note.party_snapshot.get("name") or "Unknown party"] += event["total_amount"]

    for payment in payments:
        month_key = payment.payment_date.strftime("%Y-%m")
        if payment.invoice.invoice_type == "sales":
            totals["payments_received"] += payment.amount
            series["cash_in"][month_key] += payment.amount
        else:
            totals["payments_made"] += payment.amount
            series["cash_out"][month_key] += payment.amount

    status_order = ("uploaded", "processing", "review", "verified", "paid")
    status_labels = {
        "uploaded": "Uploaded", "processing": "Processing", "review": "Needs review",
        "verified": "Verified", "paid": "Paid",
    }
    imported_gstr2b_tax = ZERO
    month_start, month_end = _first_of_month(start), _first_of_month(end)
    first_period, last_period = month_start.strftime("%Y-%m"), month_end.strftime("%Y-%m")
    latest_runs = {}
    for run in ReconciliationRun.objects.filter(
        user=user, return_period__gte=first_period, return_period__lte=last_period
    ).order_by("return_period", "-created_at"):
        latest_runs.setdefault(run.return_period, run)
    for run in latest_runs.values():
        imported_gstr2b_tax += run.records.aggregate(total=Sum("total_tax"))["total"] or ZERO

    chart_data = {
        "labels": labels,
        "monthly_sales_purchases": {
            "sales": [series["sales"][key] for key in month_keys],
            "purchases": [series["purchases"][key] for key in month_keys],
        },
        "monthly_cash": {
            "inflow": [series["cash_in"][key] for key in month_keys],
            "outflow": [series["cash_out"][key] for key in month_keys],
        },
        "invoice_status": {
            "labels": [status_labels[status] for status in status_order if status_counts[status]],
            "values": [status_counts[status] for status in status_order if status_counts[status]],
        },
        "expenses": [series["expenses"][key] for key in month_keys],
        "customers": sorted(customer_sales.items(), key=lambda item: item[1], reverse=True)[:8],
        "suppliers": sorted(supplier_purchases.items(), key=lambda item: item[1], reverse=True)[:8],
        "outstanding": [totals["receivable"], totals["payable"]],
        "gst_trend": {
            "output": [series["output_gst"][key] for key in month_keys],
            "input": [series["input_gst"][key] for key in month_keys],
        },
        "gst_breakdown": {
            "labels": ["Output CGST", "Output SGST", "Output IGST", "Input CGST", "Input SGST", "Input IGST"],
            "values": [totals["output_cgst"], totals["output_sgst"], totals["output_igst"], totals["input_cgst"], totals["input_sgst"], totals["input_igst"]],
        },
        "taxable": {
            "sales": [series["taxable_sales"][key] for key in month_keys],
            "purchases": [series["taxable_purchases"][key] for key in month_keys],
        },
    }
    return {
        "start_date": start,
        "end_date": end,
        "selected_period": preset,
        "custom_error": custom_error,
        "totals": totals,
        "gross_original": gross_original,
        "adjustment_events": adjustment_events,
        "treatment_summary":treatment_summary,
        "net_cash_flow": totals["payments_received"] - totals["payments_made"],
        "imported_gstr2b_tax": imported_gstr2b_tax,
        "status_total": sum(status_counts.values()),
        "chart_data": chart_data,
    }
