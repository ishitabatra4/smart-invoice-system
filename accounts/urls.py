
from django.urls import path
from django.views.generic import TemplateView

from . import views

urlpatterns = [
    path("register/sent/", TemplateView.as_view(template_name="accounts/registration_done.html"), name="registration_done"),
    path("verification/sent/", TemplateView.as_view(template_name="accounts/verification_sent.html"), name="verification_sent"),
    path("password-reset/complete/", TemplateView.as_view(template_name="accounts/password_reset_complete.html"), name="password_reset_complete"),
    path("password-reset/", views.AccountPasswordResetView.as_view(), name="password_reset"),
    path("password-reset/sent/", TemplateView.as_view(template_name="accounts/password_reset_done.html"), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", views.AccountPasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("password-change/", views.AccountPasswordChangeView.as_view(), name="password_change"),
    path("verify/<str:token>/", views.verify_email, name="verify_email"),
    path("verification/resend/", views.resend_verification, name="resend_verification"),
    path("register/", views.register_view, name="register"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("session/keepalive/", views.session_keepalive, name="session_keepalive"),
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("profile/", views.business_settings_view, name="profile"),
]
