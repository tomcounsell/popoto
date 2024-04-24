from .django_user_model import DjangoUser
from ..app_models.user import User
from datetime import datetime
from django.contrib.auth.backends import BaseBackend


class AuthBackend(BaseBackend):
    def authenticate(self, request, username=None, login_code=None, password=None):
        users = User.query.filter(username=username)
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
        if not len(users):
            return None
        user = users[0]
        return DjangoUser(**user.__dict__)
