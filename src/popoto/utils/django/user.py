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
        return self._password

    @password.setter
    def password(self, raw_password):
        from passlib.hash import pbkdf2_sha256

        self._password = pbkdf2_sha256.hash(raw_password)

    def check_password(self, raw_password):
        from passlib.hash import pbkdf2_sha256

        return pbkdf2_sha256.verify(raw_password, self._password)

    @property
    def serialized(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "is_staff": self.is_staff,
            "is_active": self.is_active,
        }

    @property
    def four_digit_login_code(self):
        import hashlib

        if self.email.endswith(TEST_ACCOUNT_DOMAIN):
            return TEST_ACCOUNT_LOGIN_CODE
        hash_object = hashlib.md5(
            bytes(f"{self.id}{self.email}{self.last_login}", encoding="utf-8")
        )
        return str(int(hash_object.hexdigest(), 16))[-LOGIN_CODE_LENGTH:]

    @property
    def is_agreed_to_terms(self) -> bool:
        if self.agreed_to_terms_at and self.agreed_to_terms_at > TERMS_EFFECTIVE_DATE:
            return True
        return False

    @is_agreed_to_terms.setter
    def is_agreed_to_terms(self, value: bool):
        if value is True:
            self.agreed_to_terms_at = datetime.now()
        elif value is False and self.is_agreed_to_terms:
            self.agreed_to_terms_at = None

    def __str__(self):
        return self.name or (
            self.email if self.is_email_verified else f"User {self.id}"
        )
