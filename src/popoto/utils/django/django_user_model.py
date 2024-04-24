from django.db import models
from ..app_models.user import User
from django.contrib.auth.models import AbstractUser


class DjangoUserMetaClass(models.base.ModelBase):
    def __new__(cls, name, bases, attrs, **kwargs):
        new_class = super().__new__(cls, name, bases, attrs, **kwargs)

        opts = new_class._meta
        if not hasattr(opts, "pk"):
            field = new_class._meta.get_field("username")
            opts.pk = field
        return new_class


class DjangoUser(models.Model, AbstractUser, metaclass=DjangoUserMetaClass):
    username = models.CharField(primary_key=True, max_length=150)
    password = models.CharField(max_length=128)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)

    objects = User.query

    def __init__(self, user: User = None, *args, **kwargs):
        if not user:
            user = User(**kwargs)
        for attr in user.__dict__:
            setattr(self, attr, getattr(user, attr))

    def __getattribute__(self, item):
        if item in ["save", "delete"]:
            return super().__getattribute__(item)
        return getattr(self.user, item)

    def __getattr__(self, name):
        return getattr(self.user, name)

    def __setattr__(self, name, value):
        setattr(self.user, name, value)

    def __str__(self):
        return self.username

    def save(self):
        self.user.save()

    @property
    def pk(self):
        return self.username

    # Additionally, define how Django should convert the primary key to string
    def _get_pk_val(self, meta=None):
        value = self.pk
        return value

    # Overriding how Django gets the pk value as a string for session management
    @staticmethod
    def value_to_string(obj):
        return obj.username

    class Meta:
        managed = False  # No database table creation
        verbose_name = "User"
        verbose_name_plural = "Users"

    EMAIL_FIELD = "email"
    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["username"]
