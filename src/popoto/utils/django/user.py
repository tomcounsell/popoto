"""Django-compatible User model for Redis-backed authentication.

This module provides a drop-in User model that mirrors Django's auth.User
patterns while storing data in Redis instead of a relational database.
The design philosophy prioritizes Django compatibility for seamless migration
while leveraging Redis's performance characteristics.

Why This Exists:
    Many projects start with Django and later need Redis's speed for user
    lookups (session validation, permission checks). Rather than maintaining
    two user systems, this model allows developers to keep familiar Django
    patterns while gaining Redis performance.

Design Decisions:
    1. **Django Field Parity**: Includes is_staff, is_superuser, is_active
       with identical semantics to django.contrib.auth.User.

    2. **Secure Password Storage**: Uses PBKDF2-SHA256 via passlib, matching
       Django's default hasher. Passwords are never stored in plain text.

    3. **Batteries Included**: Common patterns like email verification,
       terms acceptance, and beta testing are built-in rather than requiring
       profile extensions.

    4. **Timestampable Mixin**: Inherits created_at/updated_at from Timestampable
       for audit trails without manual tracking.

Usage:
    from popoto.utils.django.user import User

    # Create a user
    user = User(username="alice")
    user.password = "secret123"  # Auto-hashes via property setter
    user.save()

    # Authenticate
    user = User.query.get(username="alice")
    if user.check_password("secret123"):
        user.last_login = datetime.now()
        user.save()

    # Check permissions
    if user.is_staff and user.is_active:
        grant_admin_access()

Integration with Django:
    This model does NOT integrate with Django's authentication backend
    automatically. For Django projects, you'll need a custom authentication
    backend that queries this Redis model instead of the Django ORM.

See Also:
    - Timestampable mixin for automatic timestamp management
    - KeyField for understanding username uniqueness enforcement
"""
from datetime import datetime
from ...fields.field import Field
from ...fields.datetime_field import DatetimeField
from ...fields.shortcuts import KeyField, AutoKeyField, BooleanField
from ...models.base import Model
from ..mixins.timestampable import Timestampable

# Number of digits in the generated login code
LOGIN_CODE_LENGTH = 4

# Login code returned for test accounts (emails ending with TEST_ACCOUNT_DOMAIN)
TEST_ACCOUNT_DOMAIN = "@example.com"
TEST_ACCOUNT_LOGIN_CODE = "1234"

# Terms of service must be agreed to after this date to be considered valid.
# Update this date when new terms are published.
TERMS_EFFECTIVE_DATE = datetime(2024, 1, 1)


class User(Timestampable, Model):
    """A Django-compatible user model backed by Redis storage.

    This class provides user authentication and authorization primitives
    following Django conventions. It inherits from Timestampable for
    automatic created_at/updated_at tracking and from Model for Redis
    persistence.

    The dual inheritance order (Timestampable, Model) is intentional:
    Python's MRO ensures Timestampable's fields are processed first,
    then Model provides the ORM machinery.

    Field Categories:
        Identification: username (unique key), password (hashed), name fields
        Django Conventions: is_staff, is_superuser, is_active for compatibility
        Batteries Included: email verification, terms acceptance, beta flags

    Redis Key Structure:
        Users are stored with keys like "User:abc123" where abc123 is the
        auto-generated id. The username field maintains a separate unique
        index for fast lookups.

    Example:
        # Creating and saving a user
        user = User(username="bob", first_name="Bob", last_name="Smith")
        user.password = "secure_password"  # Hashed automatically
        user.is_staff = True
        user.save()

        # Querying users
        admins = User.query.filter(is_staff=True)
        user = User.query.get(username="bob")

    Attributes:
        id: Auto-generated unique identifier (UUID-based)
        username: Unique login identifier, required
        first_name: Optional given name
        last_name: Optional family name
        is_staff: Whether user can access admin areas (Django convention)
        is_superuser: Whether user has all permissions (Django convention)
        is_active: Whether user account is enabled (Django convention)
        is_email_verified: Whether email address has been confirmed
        is_beta_tester: Flag for beta feature access
        agreed_to_terms_at: Timestamp of terms acceptance
        last_login: Timestamp of most recent authentication
        created_at: Inherited from Timestampable
        updated_at: Inherited from Timestampable
    """
    id = AutoKeyField()

    # IDENTIFICATION
    username = KeyField(unique=True, null=False)
    # email = KeyField(unique=True, null=False)
    # phone_number = Field(default="")
    _password = Field(default="")

    # name = Field(default="")
    first_name = Field(default="")
    last_name = Field(default="")

    # DJANGO CONVENTIONS
    is_staff = BooleanField(default=False)  # Django convention for staff users
    is_superuser = BooleanField(default=False)  # Django convention for superusers
    is_active = BooleanField(default=True)  # Django convention, False blocks login

    # BATTERIES INCLUDED
    is_email_verified = BooleanField(default=False)
    is_beta_tester = BooleanField(default=False)
    agreed_to_terms_at = DatetimeField(null=True)
    last_login = DatetimeField(null=True)

    @property
    def password(self):
        """Retrieve the hashed password value.

        Returns the stored hash, not the original password. This is
        intentional for security - plain text passwords are never stored
        or retrievable.

        Returns:
            str: The PBKDF2-SHA256 hash of the password, or empty string
                if no password has been set.

        Note:
            To verify a password, use check_password() instead of comparing
            hash values directly.
        """
        return self._password

    @password.setter
    def password(self, raw_password):
        """Hash and store a new password.

        Automatically applies PBKDF2-SHA256 hashing when setting a password.
        This design prevents accidental plain-text storage and ensures
        consistent security practices.

        Args:
            raw_password: The plain text password to hash and store.

        Example:
            user.password = "my_secure_password"  # Hashed automatically
            user.save()

        Security Note:
            Uses passlib's pbkdf2_sha256 with default rounds (29000 as of
            passlib 1.7). This matches Django's default hasher strength.
        """
        from passlib.hash import pbkdf2_sha256

        self._password = pbkdf2_sha256.hash(raw_password)

    def check_password(self, raw_password):
        """Verify a plain text password against the stored hash.

        This is the secure way to authenticate users. The method uses
        constant-time comparison to prevent timing attacks.

        Args:
            raw_password: The plain text password to verify.

        Returns:
            bool: True if the password matches, False otherwise.

        Example:
            user = User.query.get(username="alice")
            if user and user.check_password(submitted_password):
                # Authentication successful
                user.last_login = datetime.now()
                user.save()
                create_session(user)
            else:
                raise AuthenticationError("Invalid credentials")

        Security Note:
            Always use this method rather than comparing hashes directly.
            Direct comparison is vulnerable to timing attacks.
        """
        from passlib.hash import pbkdf2_sha256

        return pbkdf2_sha256.verify(raw_password, self._password)

    @property
    def serialized(self):
        """Generate an API-safe dictionary representation of the user.

        Returns a dictionary containing only fields safe for API responses.
        Notably excludes the password hash, email verification status, and
        other sensitive internal fields.

        This property follows the principle of minimal exposure - only
        include what clients need, nothing more.

        Returns:
            dict: User data safe for JSON serialization and API responses.
                Keys: id, email, name, is_staff, is_active

        Example:
            @app.route("/api/user/me")
            def get_current_user():
                return jsonify(current_user.serialized)

        Note:
            References self.email and self.name which may be commented out
            in the current implementation. Ensure these fields exist or
            override this property in subclasses.
        """
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "is_staff": self.is_staff,
            "is_active": self.is_active,
        }

    @property
    def four_digit_login_code(self):
        """Generate a deterministic 4-digit code for passwordless login flows.

        Creates a short numeric code derived from user identity and login
        timestamp. This enables "magic link" or SMS-based authentication
        without storing additional tokens.

        Design Rationale:
            The code changes with each login (via last_login), making it
            single-use without requiring a separate token table. The
            deterministic generation means no database write is needed
            to create the code.

        Returns:
            str: A 4-digit numeric code (e.g., "7823")

        Example:
            # Email-based passwordless login
            user = User.query.get(email=submitted_email)
            code = user.four_digit_login_code
            send_email(user.email, f"Your login code is: {code}")

            # Later, when user submits the code
            if submitted_code == user.four_digit_login_code:
                user.last_login = datetime.now()  # Invalidates old code
                user.save()
                create_session(user)

        Security Considerations:
            - 4 digits provides only 10,000 combinations; implement rate
              limiting and lockout after failed attempts.
            - Test accounts (@example.com) always return "1234" for
              automated testing convenience.
            - The code is not cryptographically secure; use for low-risk
              authentication only.

        Note:
            References self.email which may be commented out in the current
            implementation.
        """
        import hashlib

        if self.email.endswith(TEST_ACCOUNT_DOMAIN):
            return TEST_ACCOUNT_LOGIN_CODE
        hash_object = hashlib.md5(
            bytes(f"{self.id}{self.email}{self.last_login}", encoding="utf-8")
        )
        return str(int(hash_object.hexdigest(), 16))[-LOGIN_CODE_LENGTH:]

    @property
    def is_agreed_to_terms(self) -> bool:
        """Check if user has accepted current terms of service.

        Returns True only if the user accepted terms after TERMS_EFFECTIVE_DATE.
        This date-based approach allows invalidating old acceptances when
        terms are updated - simply change the cutoff date.

        Design Rationale:
            Rather than a boolean flag that requires mass-updates when terms
            change, storing the acceptance timestamp allows checking against
            the terms publication date. Users who accepted old terms can be
            prompted to re-accept without touching their records.

        Returns:
            bool: True if terms were accepted after the cutoff date.

        Example:
            if not user.is_agreed_to_terms:
                redirect_to_terms_page()
        """
        if self.agreed_to_terms_at and self.agreed_to_terms_at > TERMS_EFFECTIVE_DATE:
            return True
        return False

    @is_agreed_to_terms.setter
    def is_agreed_to_terms(self, value: bool):
        """Record or revoke terms acceptance.

        Setting to True records the current timestamp as acceptance time.
        Setting to False clears any previous acceptance (useful for GDPR
        withdrawal of consent).

        Args:
            value: True to record acceptance now, False to revoke.

        Example:
            # User clicks "I agree" checkbox
            user.is_agreed_to_terms = True
            user.save()

            # User withdraws consent
            user.is_agreed_to_terms = False
            user.save()
        """
        if value is True:
            self.agreed_to_terms_at = datetime.now()
        elif value is False and self.is_agreed_to_terms:
            self.agreed_to_terms_at = None

    def __str__(self):
        """Return a human-readable representation of the user.

        Uses a privacy-aware fallback chain:
        1. Full name if available
        2. Email if verified (trusted identity)
        3. Generic "User {id}" if email is unverified (protects privacy)

        This prevents exposing unverified email addresses in logs, admin
        interfaces, or error messages where __str__ might be called.

        Returns:
            str: User's name, verified email, or anonymized identifier.

        Note:
            References self.name and self.email which may be commented out
            in the current implementation.
        """
        return self.name or (
            self.email if self.is_email_verified else f"User {self.id}"
        )
