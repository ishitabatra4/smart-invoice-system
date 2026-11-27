import calendar
import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F
from django.utils import timezone
from django.conf import settings
from invoices.manual_calculations import calculate_invoice

from accounts.models import BusinessProfile
from audit.services import record_event
from invoices.models import Invoice, InvoiceLineItem

from .models import ExpenseDetail, ExpenseCategory, RecurringExpense, RecurringExpenseGeneration


def _next_date(schedule, current):
    if schedule.frequency == "weekly":
        from datetime import timedelta
        return current + timedelta(days=7)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[schedule.frequency]
    month_index = current.year * 12 + current.month - 1 + months
    year, month = divmod(month_index, 12)
    month += 1
    day = min(schedule.schedule_day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _tax_and_total(schedule):
    result=_schedule_calculation(schedule)
    return result["taxable_amount"],result["tax_amount"],result["total_amount"]


def _schedule_calculation(schedule):
    if schedule.supplier and schedule.supplier.gstin and schedule.supplier.gstin[:2]!=schedule.supplier_state_code:
        raise ValidationError("Supplier state must match its GSTIN. Review the schedule.")
    return calculate_invoice([{"quantity":Decimal("1"),"rate":schedule.taxable_amount,"tax_rate":schedule.gst_rate,"tax_treatment":schedule.tax_treatment}],
        schedule.supplier_state_code,schedule.place_of_supply,pricing_mode="inclusive" if schedule.gst_inclusive else "exclusive",
        supply_category=schedule.supply_category,allowed_rates=settings.GST_ALLOWED_RATES)


def _save_generated_expense(schedule, due_date, actor=None):
    schedule.full_clean()
    period_key = due_date.isoformat()
    invoice_number = f"RE-{schedule.pk}-{due_date:%Y%m%d}"
    supplier = schedule.supplier
    business = schedule.business
    result=_schedule_calculation(schedule)
    taxable,tax,total=result["taxable_amount"],result["tax_amount"],result["total_amount"]
    invoice = Invoice(
        user=business.user,
        invoice_type="expense",
        invoice_number=invoice_number,
        invoice_date=due_date,
        due_date=due_date,
        supplier_party=supplier,
        supplier_name=supplier.name if supplier else schedule.title,
        supplier_gstin=supplier.gstin if supplier else "",
        taxable_amount=taxable,
        total_amount=total,
        status="verified",
    )
    for key in ("cgst_amount","sgst_amount","igst_amount"):
        setattr(invoice,key,result[key])
    invoice.calculation_version=1
    invoice.pricing_mode="inclusive" if schedule.gst_inclusive else "exclusive"
    invoice.supplier_state_code=schedule.supplier_state_code
    invoice.place_of_supply=schedule.place_of_supply
    invoice.supply_category=schedule.supply_category
    invoice.invoice_discount_type="fixed"
    invoice.invoice_discount_value=invoice.invoice_discount_amount=invoice.total_discount_amount=Decimal("0.00")
    invoice.treatment_breakdown={key:str(value) for key,value in result["treatment_breakdown"].items()}
    invoice.full_clean()
    invoice.save()
    InvoiceLineItem.objects.create(
        invoice=invoice,
        description=schedule.title,
        quantity=Decimal("1.000"),
        rate=schedule.taxable_amount,
        discount_type="fixed",discount_value=Decimal("0"),tax_treatment=schedule.tax_treatment,invoice_discount_allocated=Decimal("0"),
        cgst_amount=result["cgst_amount"],sgst_amount=result["sgst_amount"],igst_amount=result["igst_amount"],
        tax_rate=schedule.gst_rate,
        taxable_amount=taxable,
        tax_amount=tax,
        total_amount=taxable + tax,
    )
    category = ExpenseCategory.objects.get(pk=schedule.category_id, business=business)
    ExpenseDetail.objects.create(
        invoice=invoice,
        category=category,
        title=schedule.title,
        reference_number=invoice_number,
        gst_inclusive=schedule.gst_inclusive,
    )
    RecurringExpenseGeneration.objects.create(
        recurring_expense=schedule, period_key=period_key, invoice=invoice,
    )
    record_event(user=actor, business=business, action="create", module="expenses", description=f"Recurring expense {invoice.invoice_number} generated.", obj=invoice, after={"title": schedule.title, "total": invoice.total_amount, "due_date": due_date}, metadata={"schedule_id": schedule.pk, "period": period_key}, actor_label=actor.get_username() if actor else "System (recurring scheduler)")
    return invoice


def generate_due_expenses(business, as_of=None, actor=None, *, allow_backfill=False, max_occurrences=100):
    """Create every due period once; each period and schedule advance commit atomically."""
    if actor and actor.pk!=business.user_id:raise ValidationError("Choose your own business.")
    as_of = as_of or timezone.localdate()
    if not 1<=max_occurrences<=100:raise ValidationError("Generate between 1 and 100 occurrences at a time.")
    created = []
    try:
        schedule_ids=list(RecurringExpense.objects.filter(
            business=business,is_active=True,is_cancelled=False,next_due_date__lte=as_of,
        ).order_by("next_due_date","pk").values_list("pk",flat=True)[:100])
    except OperationalError as exc:
        if "locked" not in str(exc).lower():raise
        logging.getLogger(__name__).warning("Recurring generation busy; no occurrence attempted.");return []
    for schedule_id in schedule_ids:
        while True:
            if len(created)>=max_occurrences:return created
            try:
                with transaction.atomic():
                    # Acquire SQLite write ownership before reading an occurrence; PostgreSQL also serializes this business.
                    BusinessProfile.objects.filter(pk=business.pk).update(updated_at=F("updated_at"))
                    schedule = RecurringExpense.objects.select_for_update().select_related(
                        "business__user", "category", "supplier",
                    ).get(pk=schedule_id, business=business, is_active=True, is_cancelled=False)
                    due_date = schedule.next_due_date
                    if due_date > as_of:
                        break
                    if due_date<timezone.localdate() and not allow_backfill:
                        logging.getLogger(__name__).warning("Historical recurrence requires explicit catch-up (schedule_id=%s).",schedule.pk);break
                    if schedule.end_date and due_date > schedule.end_date:
                        schedule.is_active = False
                        schedule.save(update_fields=["is_active", "updated_at"])
                        break
                    period_key = due_date.isoformat()
                    try:
                        with transaction.atomic():
                            invoice = _save_generated_expense(schedule, due_date, actor=actor)
                    except IntegrityError:
                        # Only an existing generation marker means this period was
                        # already created. Other integrity failures must abort so the
                        # schedule is not advanced past a record that was never saved.
                        if not RecurringExpenseGeneration.objects.filter(
                            recurring_expense=schedule, period_key=period_key,
                        ).exists():
                            raise
                        invoice = None
                    schedule.next_due_date = _next_date(schedule, due_date)
                    if schedule.end_date and schedule.next_due_date > schedule.end_date:
                        schedule.is_active = False
                    schedule.save(update_fields=["next_due_date", "is_active", "updated_at"])
                    if invoice:
                        created.append(invoice)
            except OperationalError as exc:
                if "locked" not in str(exc).lower():raise
                logging.getLogger(__name__).warning("Recurring generation busy; no period was skipped (schedule_id=%s).",schedule_id);break
            except RecurringExpense.DoesNotExist:
                break
            except ValidationError:
                logging.getLogger(__name__).warning("Recurring expense requires GST review (schedule_id=%s).",schedule_id)
                break
    return created


def generate_due_for_all_businesses(as_of=None, *, allow_backfill=False):
    return [invoice for business in BusinessProfile.objects.iterator() for invoice in generate_due_expenses(business, as_of,allow_backfill=allow_backfill)]
