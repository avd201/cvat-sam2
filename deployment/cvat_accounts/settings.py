"""Extend CVAT production settings without replacing upstream authentication behavior."""
from cvat.settings.production import *  # noqa: F403

ACCOUNT_ADAPTER = "cvat_accounts.adapters.ApprovedAccountsAdapter"

# dj-rest-auth 7.2 does not consult allauth's is_open_for_signup on its REST
# registration view. Close that API explicitly, preserving CVAT's serializers.
REST_AUTH = {
    **REST_AUTH,  # noqa: F405
    "REGISTER_PERMISSION_CLASSES": ["cvat_accounts.permissions.RegistrationClosed"],
}
