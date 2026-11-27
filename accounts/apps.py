from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in
        from .session_policy import start_session_clock

        def start_clock(sender, request, user, **kwargs):
            if request is not None and getattr(request, "session", None) is not None:
                start_session_clock(request)

        user_logged_in.connect(start_clock, dispatch_uid="accounts.session_clock", weak=False)
