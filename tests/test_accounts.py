"""Run the isolated auth-stack suite when its optional dependencies are installed."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_account_policy_integration():
    pytest.importorskip('django')
    pytest.importorskip('dj_rest_auth')
    if not os.environ.get('CVAT_CHECKOUT'):
        pytest.skip('set CVAT_CHECKOUT to a CVAT v2.78.0 checkout')
    subprocess.run([sys.executable, str(Path(__file__).parent / 'accounts/check_policy.py')], check=True)
