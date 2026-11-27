from django.contrib import admin
from audit.services import record_event


class BusinessScopedAdmin(admin.ModelAdmin):
    owner_lookup = "business__user"

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset if request.user.is_superuser else queryset.filter(**{self.owner_lookup: request.user})

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not request.user.is_superuser:
            model = db_field.remote_field.model
            if db_field.name == "user":
                kwargs["queryset"] = model.objects.filter(pk=request.user.pk)
            elif db_field.name == "business":
                kwargs["queryset"] = model.objects.filter(user=request.user)
            elif db_field.name == "invoice":
                kwargs["queryset"] = model.objects.filter(user=request.user)
            elif db_field.name == "category":
                kwargs["queryset"] = model.objects.filter(business__user=request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        before = {}
        if change:
            original = self.get_queryset(request).get(pk=obj.pk)
            before = {field: getattr(original, field) for field in form.changed_data}
        super().save_model(request, obj, form, change)
        record_event(request=request, action="update" if change else "create", module="accounts" if obj._meta.app_label == "accounts" else "expenses",
                     description="Business record updated through administration.", obj=obj,
                     before=before, after={field: getattr(obj, field) for field in form.changed_data})
