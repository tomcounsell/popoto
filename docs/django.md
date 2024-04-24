# Django Utils Documentation

This documentation provides a guide on how to use the Django utilities provided in the `popoto` project.

## Authentication Backend

The `AuthBackend` class is a custom authentication backend that authenticates users based on their username and either a login code or password.

To use `AuthBackend`, add it to the `AUTHENTICATION_BACKENDS` setting in your Django settings file:

```python
AUTHENTICATION_BACKENDS = ["popoto.utils.django.auth_backend.AuthBackend"]
```

## Session Serializer

The `UserSerializer` class is a custom session serializer that serializes and deserializes user session data.

To use `UserSerializer`, set it as the `SESSION_SERIALIZER` in your Django settings file:

```python
SESSION_SERIALIZER = "popoto.utils.django.user_serializer.UserSerializer"
```

## Django User Model

The `DjangoUser` class is a custom user model that extends Django's `AbstractUser` model. It uses the username as the primary key and includes fields for password, active status, staff status, and superuser status.

To use `DjangoUser`, you need to ensure that the `popoto` application is included in your `INSTALLED_APPS` setting. However, you should not set `AUTH_USER_MODEL` to `"popoto.DjangoUser"` as this model is not part of an installed Django application.

Instead, you can use the `User` model in `user.py` directly, subclass it, or copy it depending on the specific needs of your project. If you want to use it as the user model for Django's authentication system, you would need to ensure it includes certain required fields and methods as specified in Django's documentation on [custom user models](https://docs.djangoproject.com/en/3.2/topics/auth/customizing/#specifying-a-custom-user-model). 

## Session Middleware

The `SessionMiddleware` class is a custom middleware that processes requests and responses. It checks if a user is authenticated and sets a custom session key.

To use `SessionMiddleware`, add it to the `MIDDLEWARE` setting in your Django settings file:

```python
MIDDLEWARE = [
    ...
    "popoto.utils.django.session_middleware.SessionMiddleware",
    ...
]
```

## User Admin

The `UserAdmin` class is a custom admin model for the `DjangoUser` model. It includes a form for editing user instances and defines the fields to display in the admin list view.

To register `UserAdmin` with the Django admin site, add the following to your `admin.py` file:

```python
from django.contrib import admin
from popoto.utils.django.user_admin import UserAdmin
from popoto.utils.django.django_user_model import DjangoUser

admin.site.register(DjangoUser, UserAdmin)
```

Please note that the paths provided in the examples are relative to the root of your Django project. Adjust them as necessary based on your project structure.

