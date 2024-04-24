from django.contrib.sessions.serializers import JSONSerializer
from .auth_backend import AuthBackend


class PopotoUserSerializer(JSONSerializer):
    def serialize(self, obj):
        # Serialize the user information manually
        user = obj.get("_auth_user")
        user._meta.pk.value_to_string = lambda x: x.username
        if user and hasattr(user, "username"):
            # Store user identifier and the authentication backend used
            obj["_auth_user_id"] = user.username
            obj["_auth_user_backend"] = (
                AuthBackend.__module__ + "." + AuthBackend.__name__
            )
        return super().serialize(obj)

    def deserialize(self, data):
        # Deserialize the session data
        data = super().deserialize(data)
        user_id = data.get("_auth_user_id")
        backend_path = data.get("_auth_user_backend")

        # Recreate the user object from username
        if user_id and backend_path:
            from django.utils.module_loading import import_string

            backend = import_string(backend_path)
            user = backend().get_user(user_id)
            data["_auth_user"] = user
        return data
