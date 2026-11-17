from rest_framework.permissions import BasePermission


class RegistrationClosed(BasePermission):
    message = "Self-registration is disabled. Contact your administrator for an account."

    def has_permission(self, request, view):
        return False
