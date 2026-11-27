"""Owned, versioned extraction evidence and append-only human confirmation snapshots."""
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class InvoiceExtractionAttempt(models.Model):
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    business=models.ForeignKey('accounts.BusinessProfile',on_delete=models.PROTECT)
    batch=models.ForeignKey('invoices.InvoiceBatch',null=True,blank=True,on_delete=models.PROTECT,related_name='extraction_attempts')
    invoice=models.ForeignKey('invoices.Invoice',null=True,blank=True,on_delete=models.PROTECT,related_name='extraction_attempts')
    source_file=models.FileField(upload_to='extractions/%Y/%m/')
    original_name=models.CharField(max_length=255)
    file_sha256=models.CharField(max_length=64)
    status=models.CharField(max_length=12,default='uploaded',choices=[(v,v.title()) for v in ('uploaded','processing','review','partial','failed','blocked')])
    method=models.CharField(max_length=30,blank=True)
    result=models.JSONField(default=dict)
    duplicate_signals=models.JSONField(default=list)
    error_category=models.CharField(max_length=40,blank=True)
    started_at=models.DateTimeField(null=True,blank=True)
    completed_at=models.DateTimeField(null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['-created_at','-pk']
        indexes=[models.Index(fields=['business','created_at'],name='extraction_business_date_idx'),models.Index(fields=['business','file_sha256'],name='extraction_business_hash_idx')]
        constraints=[models.CheckConstraint(condition=Q(status__in=['uploaded','processing','review','partial','failed','blocked']),name='extraction_valid_status')]
    def clean(self):
        if self.business_id and self.business.user_id!=self.user_id:raise ValidationError('Extraction must belong to its business.')
        if self.invoice_id and self.invoice.user_id!=self.user_id:raise ValidationError('Choose your own invoice.')
        if self.batch_id and self.batch.user_id!=self.user_id:raise ValidationError('Choose your own upload batch.')
    def save(self,*args,**kwargs):
        if self.pk:
            old=type(self).objects.get(pk=self.pk)
            if old.status not in {'uploaded','processing'}:
                for field in self._meta.fields:
                    if field.name=='invoice' and old.invoice_id is None:continue
                    if getattr(self,field.attname)!=getattr(old,field.attname):raise ValidationError('Completed extraction evidence is immutable; create a new attempt.')
        super().save(*args,**kwargs)


class InvoiceExtractionReview(models.Model):
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    business=models.ForeignKey('accounts.BusinessProfile',on_delete=models.PROTECT)
    invoice=models.ForeignKey('invoices.Invoice',on_delete=models.PROTECT,related_name='extraction_reviews')
    attempt=models.ForeignKey(InvoiceExtractionAttempt,on_delete=models.PROTECT,related_name='reviews')
    review_key=models.UUIDField(default=uuid.uuid4,unique=True,editable=False)
    snapshot=models.JSONField()
    confirmed_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['-confirmed_at','-pk']
        indexes=[models.Index(fields=['invoice','confirmed_at'],name='extraction_review_date_idx')]
    def clean(self):
        if self.business.user_id!=self.user_id or self.invoice.user_id!=self.user_id or self.attempt.user_id!=self.user_id:raise ValidationError('Review must belong to its business owner.')
        if self.attempt.invoice_id!=self.invoice_id:raise ValidationError('Review must refer to its extraction invoice.')
    def save(self,*args,**kwargs):
        if self.pk:raise ValidationError('Confirmed review history cannot be overwritten.')
        super().save(*args,**kwargs)
