
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("analytics/", include("analytics.urls")),
    path("invoices/", include("invoices.urls")),
    path("expenses/", include("expenses.urls")),
    path("audit/", include("audit.urls")),
    path("tax/", include("taxation.urls")),
    path("", RedirectView.as_view(pattern_name="dashboard", permanent=False)),
    
]
