from django.contrib import admin
from django.forms import ModelForm as DjangoModelForm
from .django_user_model import DjangoUser


class UserAdminForm(DjangoModelForm):
    class Meta:
        model = DjangoUser
        fields = ["username", "password", "is_staff", "is_superuser"]


class UserAdmin(admin.ModelAdmin):
    model = DjangoUser
    list_display = ("username", "is_staff", "is_active")
    search_fields = ("username",)
