
from django.urls import path
from . import views

urlpatterns = [
    path("", views.invoice_list_view, name="invoice_list"),
    path("upload/", views.invoice_upload_view, name="invoice_upload"),
    path("<int:invoice_id>/delete/",views.invoice_delete_view,name="invoice_delete",),
    path("<int:invoice_id>/", views.invoice_detail_view, name="invoice_detail"),
]
