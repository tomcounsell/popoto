"""
Django Authentication Backend for Popoto User Models.

This module bridges Popoto's Redis-backed User model with Django's authentication
framework, enabling Django projects to use Redis as their user store while maintaining
full compatibility with Django's auth ecosystem (login views, decorators, middleware).

Design Philosophy:
    Django's authentication system is backend-agnostic by design, requiring only that
    backends implement `authenticate()` and `get_user()`. This module leverages that
    abstraction to swap out the traditional database-backed User model for Popoto's
    Redis-persisted User, gaining Redis's performance benefits for session-heavy
    applications without sacrificing Django's battle-tested auth patterns.

Integration:
    Add to your Django settings.py:
        AUTHENTICATION_BACKENDS = ['popoto.utils.django.auth_backend.PopotoAuthBackend']

    Then use Django's standard auth functions:
        from django.contrib.auth import authenticate, login
        user = authenticate(request, email='user@example.com', password='secret')
        if user:
            login(request, user)

Trade-offs:
    - Supports dual authentication: traditional passwords AND time-based login codes
    - No Django ORM integration means no built-in admin, permissions, or groups
    - Users are queried by email (not username) to support modern auth patterns
"""
from datetime import datetime
from django.contrib.auth.backends import BaseBackend
from .user import User


class PopotoAuthBackend(BaseBackend):
    """
    Custom Django authentication backend that authenticates against Popoto's Redis User model.

    This backend enables Django applications to authenticate users stored in Redis rather
    than a traditional SQL database. It supports two authentication methods:

    1. Password authentication: Standard secure password verification using PBKDF2-SHA256
    2. Login code authentication: Time-sensitive four-digit codes for passwordless login

    The dual authentication approach accommodates both traditional web apps (password-based)
    and mobile/API scenarios where magic links or SMS codes provide better UX.

    Attributes:
        None - stateless backend; all state lives in Redis

    Example:
        # In settings.py
        AUTHENTICATION_BACKENDS = ['popoto.utils.django.auth_backend.PopotoAuthBackend']

        # In your view
        user = authenticate(request, email='user@example.com', password='secret')
        # OR
        user = authenticate(request, email='user@example.com', login_code='1234')
    """

    def authenticate(self, request, email=None, login_code=None, password=None):
        """
        Authenticate a user by email with either password or login code.

        This method follows Django's authentication contract while adding flexibility
        for passwordless authentication flows. It queries Redis for the user by email,
        validates credentials, and updates the last_login timestamp on success.

        The method accepts both `password` and `login_code` to support:
        - Traditional password-based login for web applications
        - Passwordless login via time-sensitive codes (magic links, SMS verification)

        Only one credential type needs to match for authentication to succeed.

        Args:
            request: Django HttpRequest object (required by Django's auth contract,
                     but not used in this implementation)
            email: User's email address used as the lookup key
            login_code: Optional four-digit code for passwordless authentication
            password: Optional password for traditional authentication

        Returns:
            User: The authenticated Popoto User instance if credentials are valid
                  and the user is active
            None: If user not found, inactive, or credentials invalid

        Side Effects:
            Updates user.last_login timestamp on successful authentication
        """
        users = User.query.filter(email=email)
        if len(users) == 0:
            return None
        user = users[0]
        if not user.is_active:
            return None
        if any(
            [
                login_code and login_code == user.four_digit_login_code,
                password and user.check_password(password),
            ]
        ):
            user.last_login = datetime.now()
            user.save()
            return user
        return None

    def get_user(self, user_id):
        """
        Retrieve a user by their unique ID for session restoration.

        Django calls this method on every request (via AuthenticationMiddleware) to
        restore the authenticated user from the session. The user_id is stored in the
        session after successful login and used here to fetch the full User object.

        This method enforces the is_active check, meaning a user can be deactivated
        (e.g., account suspended) and will be immediately logged out on their next
        request, even with a valid session.

        Args:
            user_id: The unique identifier for the user (stored in session as
                     request.session[SESSION_KEY])

        Returns:
            User: The active Popoto User instance if found and active
            None: If user not found or user.is_active is False

        Note:
            This is called on EVERY authenticated request, so it benefits significantly
            from Redis's low-latency reads compared to SQL databases.
        """
        users: list = User.query.filter(id=user_id)
        if len(users) == 0:
            return None
        elif users[0].is_active:
            return users[0]
        return None
