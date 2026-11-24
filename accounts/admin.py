from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as Base

from .models import User


@admin.register(User)
class UserAdmin(Base):
    ordering = ("-date_joined",)
    list_display = ("email", "full_name", "role", "phone", "is_email_verified", "role_verified", "is_active")
    list_filter = ("role", "is_email_verified", "role_verified", "is_active")
    search_fields = ("email", "full_name", "phone", "roll_or_emp_no")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "phone", "role", "department", "roll_or_emp_no", "organisation", "gate")}),
        ("Verification", {"fields": ("is_email_verified", "role_verified")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "role", "password1", "password2")}),)
