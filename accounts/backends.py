"""Email authentication using the existing Django user table and password hashes."""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models.functions import Lower, Trim


def normalize_email(value):
    return (value or "").strip().lower()


def email_candidates(email):
    # Also match legacy mixed-case/space-padded addresses without rewriting them.
    return get_user_model().objects.annotate(
        login_email=Lower(Trim("email"))
    ).filter(login_email=normalize_email(email)).exclude(login_email="")


class EmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, email=None, **kwargs):
        if email is None or password is None:
            return None
        users = list(email_candidates(email))
        if not users:
            get_user_model()().set_password(password)
            return None
        # Legacy email duplicates remain separate accounts. Only an unambiguous
        # password match may select one; never choose the first matching row.
        matches = [user for user in users if user.check_password(password)]
        if len(matches) != 1:
            return None
        user = matches[0]
        if self.user_can_authenticate(user):
            return user
        if request is not None:
            from .models import AccountVerification
            request.email_unverified = AccountVerification.objects.filter(
                user=user, verified_at__isnull=True
            ).exists()
        return None
