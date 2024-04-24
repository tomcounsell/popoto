from django.utils.deprecation import MiddlewareMixin
from ..django.django_user_model import DjangoUser


class SessionMiddleware(MiddlewareMixin):
    def process_request(self, request):
        # Check if user is authenticated and set the custom session key
        if request.user.is_authenticated:
            # Ensure this is only done for instances of your custom user
            if isinstance(request.user, DjangoUser):
                request.session["_auth_user_id"] = request.user.username

    def process_response(self, request, response):
        # You can add any additional response handling here if needed
        return response
