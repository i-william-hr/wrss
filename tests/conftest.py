#!/usr/bin/env python3
"""
Jinja2 template for pytest conftest.py
Follows the sso/wg_gui patterns: temp config, monkeypatched secrets, no live network
"""

import os
import sys
import tempfile
import shutil

# Point the service at a scratch config dir and fixed credentials *before* it is
# imported, since it may read config at module import time.
# The password/keys here are throwaway, not the live ones.
_TMP_CONF = tempfile.mkdtemp(prefix="wrss-test-")
os.environ["WRSS_CONF_DIR"] = _TMP_CONF
os.environ["WRSS_INSECURE_COOKIE"] = "1"
# Add other env vars as needed based on .env.example

# Add project root to path so we can import the main module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wrss  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP_CONF, ignore_errors=True)


# Common fixtures
import pytest

@pytest.fixture
def client():
    """Test client for the Flask/FastAPI app."""
    wrss.app.config["TESTING"] = True
    with wrss.app.test_client() as client:
        yield client


@pytest.fixture
def auth_client(client):
    """Authenticated test client (if app has auth)."""
    # Override in specific test modules
    return client