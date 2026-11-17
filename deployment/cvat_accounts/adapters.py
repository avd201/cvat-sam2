from cvat.apps.iam.adapters import DefaultAccountAdapterEx


class ApprovedAccountsAdapter(DefaultAccountAdapterEx):
    """Close public signup; Django Admin and management commands still create users."""

    def is_open_for_signup(self, request):
        # Do not exempt authenticated users or invitation query parameters. Organization
        # invitations grant membership to accounts that an administrator already created.
        return False
