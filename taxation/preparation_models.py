import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q, F
from django.core.exceptions import ValidationError

class ImmutableTaxQuerySet(models.QuerySet):

    def update(self, **kwargs):
        raise ValidationError('Tax working-paper history is immutable.')

    def delete(self):
        raise ValidationError('Tax working-paper history is retained.')

    def bulk_update(self, *args, **kwargs):
        raise ValidationError('Tax working-paper history is immutable.')

class TaxPeriod(models.Model):
    objects = ImmutableTaxQuerySet.as_manager()
    KINDS = [
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
        ('financial_year', 'Financial year'),
        ('custom', 'Custom date range'),
    ]
    business = models.ForeignKey('accounts.BusinessProfile', on_delete=models.PROTECT, related_name='tax_periods')
    kind = models.CharField(max_length=16, choices=KINDS)
    start_date = models.DateField()
    end_date = models.DateField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-start_date', '-pk']
        constraints = [
            models.UniqueConstraint(fields=['business', 'start_date', 'end_date'], name='tax_period_unique_dates'),
            models.CheckConstraint(condition=Q(end_date__gte=F('start_date')), name='tax_period_ordered_dates'),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Period boundaries are immutable; choose another period.')
        if self.business.user_id != self.created_by_id:
            raise ValidationError('Choose your own business.')
        from .preparation import validate_period
        validate_period(self.kind, self.start_date, self.end_date)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Tax periods are retained.')

    @property
    def status(self):
        paper = self.papers.order_by('-pk').first()
        return paper.status if paper else 'draft'

class TaxWorkingPaper(models.Model):
    KINDS = [
        ('gstr1', 'GSTR-1 working summary'),
        ('gstr3b', 'GSTR-3B working summary'),
        ('itc', 'ITC comparison'),
        ('liability', 'GST component-difference estimate'),
        ('income_summary', 'Income/cost working summary'),
        ('income_tax', 'Income-tax estimate'),
    ]
    period = models.ForeignKey(TaxPeriod, on_delete=models.PROTECT, related_name='papers')
    kind = models.CharField(max_length=16, choices=KINDS)
    revision = models.PositiveIntegerField()
    previous = models.OneToOneField('self', null=True, blank=True, on_delete=models.PROTECT, related_name='next_revision')
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    prepared_at = models.DateTimeField(auto_now_add=True)
    request_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    calculation_version = models.CharField(max_length=40)
    inputs = models.JSONField()
    data = models.JSONField()
    source_fingerprint = models.CharField(max_length=64)
    rule_fingerprint = models.CharField(max_length=64)
    objects = ImmutableTaxQuerySet.as_manager()

    class Meta:
        ordering = ['-pk']
        constraints = [
            models.UniqueConstraint(fields=['period', 'kind', 'revision'], name='tax_paper_unique_revision'),
            models.CheckConstraint(condition=Q(revision__gte=1), name='tax_paper_positive_revision'),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Prepared calculations cannot be overwritten; create an explicit revision.')
        if self.period.business.user_id != self.prepared_by_id:
            raise ValidationError('Choose your own period.')
        if self.previous_id and (self.previous.period_id != self.period_id or self.previous.kind != self.kind or self.previous.revision + 1 != self.revision):
            raise ValidationError('Revision must follow the same period and return type.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Working papers are retained.')

    @property
    def status(self):
        event = self.events.order_by('-pk').first()
        return event.status if event else 'prepared'

class TaxWorkingPaperEvent(models.Model):
    STATUSES = [
        ('prepared', 'Prepared'),
        ('reviewed', 'Reviewed working paper'),
        ('archived', 'Archived'),
        ('superseded', 'Superseded by explicit revision'),
    ]
    paper = models.ForeignKey(TaxWorkingPaper, on_delete=models.PROTECT, related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=12, choices=STATUSES)
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = ImmutableTaxQuerySet.as_manager()

    class Meta:
        ordering = ['-pk']
        constraints = [
            models.CheckConstraint(condition=Q(status__in=['prepared', 'reviewed', 'archived', 'superseded']), name='tax_event_valid_status'),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Lifecycle events are append-only.')
        if self.paper.period.business.user_id != self.actor_id:
            raise ValidationError('Choose your own working paper.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Lifecycle history is retained.')
