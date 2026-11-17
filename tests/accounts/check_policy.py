"""Isolated Django auth-stack integration checks (no CVAT database or GPU).

Uses the real CVAT adapter from CVAT_CHECKOUT and pinned dj-rest-auth views.
A minimal production-settings fixture replaces CVAT's database/OPA dependencies;
this is not a full CVAT server acceptance test.
"""
import os
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'deployment'), os.environ['CVAT_CHECKOUT']]
production = types.ModuleType('cvat.settings.production')
production.SECRET_KEY = 'isolated-test-only'
production.REST_AUTH = {'SESSION_LOGIN': True, 'OLD_PASSWORD_FIELD_ENABLED': True}
sys.modules['cvat.settings.production'] = production

from cvat_accounts import settings as policy
from django.conf import settings
settings.configure(
    SECRET_KEY=policy.SECRET_KEY,
    ACCOUNT_ADAPTER=policy.ACCOUNT_ADAPTER,
    REST_AUTH=policy.REST_AUTH,
    ALLOWED_HOSTS=['testserver'],
    ROOT_URLCONF=__name__,
    DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}},
    INSTALLED_APPS=['django.contrib.auth', 'django.contrib.contenttypes',
                    'django.contrib.sessions', 'django.contrib.sites', 'rest_framework',
                    'rest_framework.authtoken', 'allauth', 'allauth.account', 'allauth.socialaccount', 'dj_rest_auth'],
    MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware',
                'django.contrib.auth.middleware.AuthenticationMiddleware',
                'allauth.account.middleware.AccountMiddleware'],
    AUTHENTICATION_BACKENDS=['django.contrib.auth.backends.ModelBackend',
                             'allauth.account.auth_backends.AuthenticationBackend'],
    ACCOUNT_EMAIL_VERIFICATION='none',
    ACCOUNT_LOGIN_METHODS={'username', 'email'},
    SITE_ID=1,
)
import django
django.setup()
from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory
from django.urls import path
from allauth.account.adapter import get_adapter
from dj_rest_auth.registration.views import RegisterView
from dj_rest_auth.views import LoginView
from rest_framework.test import APIClient

urlpatterns = [path('api/auth/register', RegisterView.as_view()),
               path('api/auth/login', LoginView.as_view())]


class AccountPolicyTests(TestCase):
    def test_rest_registration_rejected_without_creating_users_or_tokens(self):
        from rest_framework.authtoken.models import Token
        client = APIClient()
        for content_type in ['json', 'multipart']:
            response = client.post('/api/auth/register', {
                'username': 'outsider', 'email': 'outsider@example.org',
                'password1': 'Test-only-pass-92841!', 'password2': 'Test-only-pass-92841!',
            }, format=content_type)
            self.assertIn(response.status_code, (401, 403))
        self.assertEqual(get_user_model().objects.count(), 0)
        self.assertEqual(Token.objects.count(), 0)

    def test_authenticated_admin_cannot_use_public_signup_even_with_invitation(self):
        user = get_user_model().objects.create_superuser('admin', 'admin@example.org', 'secret')
        client = APIClient()
        client.force_authenticate(user)
        response = client.post('/api/auth/register?invitation=untrusted', {
            'username': 'outsider', 'email': 'outsider@example.org',
            'password1': 'Test-only-pass-92841!', 'password2': 'Test-only-pass-92841!',
        }, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_allauth_signup_is_closed(self):
        request = RequestFactory().post('/accounts/signup/')
        self.assertFalse(get_adapter(request).is_open_for_signup(request))

    def test_admin_provisioned_account_can_login(self):
        user = get_user_model().objects.create_user('annotator', 'annotator@example.org', 'Test-only-pass-92841!')
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        response = APIClient().post('/api/auth/login', {
            'username': 'annotator', 'password': 'Test-only-pass-92841!',
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('key', response.data)

    def test_inactive_account_cannot_login(self):
        get_user_model().objects.create_user('inactive', password='Test-only-pass-92841!', is_active=False)
        response = APIClient().post('/api/auth/login', {
            'username': 'inactive', 'password': 'Test-only-pass-92841!',
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_preserves_upstream_settings_and_adapter(self):
        from cvat.apps.iam.adapters import DefaultAccountAdapterEx
        self.assertTrue(settings.REST_AUTH['OLD_PASSWORD_FIELD_ENABLED'])
        self.assertIsInstance(get_adapter(), DefaultAccountAdapterEx)
        self.assertEqual(production.REST_AUTH, {'SESSION_LOGIN': True, 'OLD_PASSWORD_FIELD_ENABLED': True})


if __name__ == '__main__':
    from django.test.runner import DiscoverRunner
    failures = DiscoverRunner(verbosity=2).run_tests([__name__])
    sys.exit(bool(failures))
