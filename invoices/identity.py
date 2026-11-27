"""Query-side normalization matching the application's supported edge whitespace."""
from django.db.models import Func, Value
from django.db.models.functions import Lower
from .models import NORMALIZATION_WHITESPACE


def normalized_identity(field):
    # Trim's `chars=` keyword is not a SQL argument in Django; pass characters explicitly.
    return Lower(Func(Func(field, Value(NORMALIZATION_WHITESPACE), function='LTRIM'),
                      Value(NORMALIZATION_WHITESPACE), function='RTRIM'))
