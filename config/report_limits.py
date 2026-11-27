"""Shared interactive report limits; calendar bounds match manual invoice forms."""
from datetime import date
from django.core.exceptions import ValidationError

MIN_REPORT_DATE = date(1900, 1, 1)
MAX_REPORT_DATE = date(9998, 12, 31)
MAX_REPORT_DAYS = 3660
MAX_MONTH_BUCKETS = 122
MAX_REPORT_INVOICES = 2000
MAX_REPORT_NOTES = 1000
MAX_REPORT_RELATED_ROWS = 10000
MAX_EXPORT_ROWS = 10000


def validate_report_range(start, end, *, max_days=MAX_REPORT_DAYS):
    if not MIN_REPORT_DATE <= start <= MAX_REPORT_DATE or not MIN_REPORT_DATE <= end <= MAX_REPORT_DATE:
        raise ValidationError('Choose dates between 1900-01-01 and 9998-12-31.')
    if start > end or (end - start).days > max_days:
        raise ValidationError(f'Choose an ordered range of at most {max_days + 1:,} inclusive days.')
