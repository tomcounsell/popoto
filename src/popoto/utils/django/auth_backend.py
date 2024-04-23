from datetime import datetime
from django.contrib.auth.backends import BaseBackend
from .user import User


class PopotoAuthBackend(BaseBackend):
    def authenticate(self, request, email=None, login_code=None, password=None):
        # Assuming that there's a method 'get_by_email' that retrieves a user by email
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
        users: list = User.query.filter(id=user_id)
        if len(users) == 0:
            return None
        elif users[0].is_active:
            return users[0]
        return None
