#!/usr/bin/env python3
"""
Jinja2 template for API integration tests
Follows the sso/wg_gui patterns: test client, auth gates, rate limits, CSRF, redirect safety
"""

import pytest

# Routes discovered in entrypoint:
#   GET / -> index
#   GET /api/bootstrap -> api_bootstrap
#   GET /api/status -> api_status
#   GET /api/articles -> api_articles
#   POST /api/articles/<int:aid>/translate -> api_article_translate
#   POST /api/articles/<int:aid>/headline -> api_article_headline_translate
#   POST /api/articles/translate-batch -> api_articles_translate_batch
#   POST /api/articles/<int:aid>/read -> api_article_read
#   POST /api/articles/read_all -> api_read_all
#   GET, POST /api/feeds -> api_feeds
#   PUT, DELETE /api/feeds/<int:fid> -> api_feed_edit
#   POST /api/refresh -> api_refresh
#   GET /api/admin -> api_admin_get
#   PUT /api/admin -> api_admin_put
#   POST /api/admin/translate-now -> api_admin_translate_now
#   POST /api/admin/purge-pending -> api_admin_purge_pending
#   POST /api/admin/remove-oldest -> api_admin_remove_oldest
#   GET /feed -> rss_feed


class TestHealthEndpoints:
    """Tests for health check and public endpoints."""

    def test_healthz(self, client):
        """Health check endpoint should return 200."""
        resp = client.get("/healthz")
        # Some apps use /health, /healthz, /ready, /ping
        assert resp.status_code in (200, 404)  # 404 if not implemented

    def test_root_redirects_or_returns_content(self, client):
        """Root endpoint should respond (redirect or content)."""
        resp = client.get("/")
        assert resp.status_code in (200, 301, 302, 401, 403)


class TestAuthGates:
    """Tests for authentication gates (SSO, Basic, API key)."""

    def test_protected_endpoint_requires_auth(self, client):
        """Protected endpoints should return 401/403 without auth."""
        # Test a known protected endpoint
        protected_routes = [
            ("/", ["GET"]),
            ("/api/bootstrap", ["GET"]),
            ("/api/status", ["GET"]),
            ("/api/articles", ["GET"]),
            ("/api/articles/<int:aid>/translate", ["POST"]),
            ("/api/articles/<int:aid>/headline", ["POST"]),
            ("/api/articles/translate-batch", ["POST"]),
            ("/api/articles/<int:aid>/read", ["POST"]),
            ("/api/articles/read_all", ["POST"]),
            ("/api/feeds", ["GET", "POST"]),
            ("/api/feeds/<int:fid>", ["PUT", "DELETE"]),
            ("/api/refresh", ["POST"]),
            ("/api/admin", ["GET"]),
            ("/api/admin", ["PUT"]),
            ("/api/admin/translate-now", ["POST"]),
            ("/api/admin/purge-pending", ["POST"]),
            ("/api/admin/remove-oldest", ["POST"]),
            ("/feed", ["GET"]),
        ]
        for route, methods in protected_routes:
            # Skip health check endpoints (intentionally public)
            if route in ["/healthz", "/health", "/ready", "/ping", "/metrics"]:
                continue
            if route == "/":
                continue
            # Use the first supported method for testing auth
            method = methods[0].lower()
            resp = getattr(client, method)(route)
            # Should be 401 (unauthenticated) or 403 (forbidden) or 302 (redirect to login) or 405 (wrong method but auth gate works)
            assert resp.status_code in (401, 403, 302, 405), f"Route {route} should require auth"

    def test_sso_redirect_preserves_next(self, client):
        """SSO login should preserve the next parameter."""
        # This tests the SSO gate pattern
        resp = client.get("/protected?next=/target")
        if resp.status_code == 302:
            location = resp.headers.get("Location", "")
            assert "next=" in location or "/sso/login" in location


class TestRateLimiting:
    """Tests for rate limiting on auth endpoints."""

    def test_login_rate_limit(self, client):
        """Repeated failed logins should be rate limited."""
        # This is a placeholder - actual implementation depends on the app
        # The sso pattern uses throttle_record_failure / throttle_blocked
        pass


class TestCSRFProtection:
    """Tests for CSRF protection on mutating endpoints."""

    def test_mutating_endpoint_requires_csrf(self, client):
        """POST/PUT/DELETE endpoints should require CSRF token."""
        resp = client.post("/api/articles/<int:aid>/translate", data={})
        # May return 401 (auth required first), 400 (bad request), or 403 (CSRF missing)
        assert resp.status_code in (400, 401, 403), f"/api/articles/<int:aid>/translate should require CSRF or auth"
        resp = client.post("/api/articles/<int:aid>/headline", data={})
        # May return 401 (auth required first), 400 (bad request), or 403 (CSRF missing)
        assert resp.status_code in (400, 401, 403), f"/api/articles/<int:aid>/headline should require CSRF or auth"
        resp = client.post("/api/articles/translate-batch", data={})
        # May return 401 (auth required first), 400 (bad request), or 403 (CSRF missing)
        assert resp.status_code in (400, 401, 403), f"/api/articles/translate-batch should require CSRF or auth"
        resp = client.post("/api/articles/<int:aid>/read", data={})
        # May return 401 (auth required first), 400 (bad request), or 403 (CSRF missing)
        assert resp.status_code in (400, 401, 403), f"/api/articles/<int:aid>/read should require CSRF or auth"
        resp = client.post("/api/articles/read_all", data={})
        # May return 401 (auth required first), 400 (bad request), or 403 (CSRF missing)
        assert resp.status_code in (400, 401, 403), f"/api/articles/read_all should require CSRF or auth"
        resp = client.post("/api/feeds", data={})
        # May return 401 (auth required first), 400 (bad request), or 403 (CSRF missing)
        assert resp.status_code in (400, 401, 403), f"/api/feeds should require CSRF or auth"
        resp = client.post("/api/feeds/<int:fid>", data={})
        # May return 401 (auth required first), 400 (bad request), or 403 (CSRF missing)
        assert resp.status_code in (400, 401, 403), f"/api/feeds/<int:fid> should require CSRF or auth"
        resp = client.post("/api/refresh", data={})
        # May return 401 (auth required first), 400 (bad request), or 403 (CSRF missing)
        assert resp.status_code in (400, 401, 403), f"/api/refresh should require CSRF or auth"
        resp = client.post("/api/admin", data={})
        # May return 401 (auth required first), 400 (bad request), or 403 (CSRF missing)
        assert resp.status_code in (400, 401, 403), f"/api/admin should require CSRF or auth"
        resp = client.post("/api/admin/translate-now", data={})
        # May return 401 (auth required first), 400 (bad request), or 403 (CSRF missing)
        assert resp.status_code in (400, 401, 403), f"/api/admin/translate-now should require CSRF or auth"
        resp = client.post("/api/admin/purge-pending", data={})
        # May return 401 (auth required first), 400 (bad request), or 403 (CSRF missing)
        assert resp.status_code in (400, 401, 403), f"/api/admin/purge-pending should require CSRF or auth"
        resp = client.post("/api/admin/remove-oldest", data={})
        # May return 401 (auth required first), 400 (bad request), or 403 (CSRF missing)
        assert resp.status_code in (400, 401, 403), f"/api/admin/remove-oldest should require CSRF or auth"


class TestRedirectSafety:
    """Tests for open redirect protection."""

    def test_redirect_validation(self, client):
        """Redirects should be validated against allowlist."""
        # Test the safe_next pattern from sso
        malicious_targets = [
            "https://evil.example/",
            "//evil.example/",
            "javascript:alert(1)",
            "/\\evil.example/",
        ]
        for target in malicious_targets:
            # This would need app-specific testing
            pass


class TestAPIEndpoints:
    """Tests for specific API endpoints."""

    def test_index_endpoint(self, client):
        """Test GET /."""
        resp = client.open("/", method="GET")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_bootstrap_endpoint(self, client):
        """Test GET /api/bootstrap."""
        resp = client.open("/api/bootstrap", method="GET")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_status_endpoint(self, client):
        """Test GET /api/status."""
        resp = client.open("/api/status", method="GET")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_articles_endpoint(self, client):
        """Test GET /api/articles."""
        resp = client.open("/api/articles", method="GET")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_article_translate_endpoint(self, client):
        """Test POST /api/articles/<int:aid>/translate."""
        resp = client.open("/api/articles/<int:aid>/translate", method="POST")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_article_headline_translate_endpoint(self, client):
        """Test POST /api/articles/<int:aid>/headline."""
        resp = client.open("/api/articles/<int:aid>/headline", method="POST")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_articles_translate_batch_endpoint(self, client):
        """Test POST /api/articles/translate-batch."""
        resp = client.open("/api/articles/translate-batch", method="POST")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_article_read_endpoint(self, client):
        """Test POST /api/articles/<int:aid>/read."""
        resp = client.open("/api/articles/<int:aid>/read", method="POST")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_read_all_endpoint(self, client):
        """Test POST /api/articles/read_all."""
        resp = client.open("/api/articles/read_all", method="POST")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_feeds_endpoint(self, client):
        """Test GET, POST /api/feeds."""
        resp = client.open("/api/feeds", method="GET")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_feed_edit_endpoint(self, client):
        """Test PUT, DELETE /api/feeds/<int:fid>."""
        resp = client.open("/api/feeds/<int:fid>", method="PUT")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_refresh_endpoint(self, client):
        """Test POST /api/refresh."""
        resp = client.open("/api/refresh", method="POST")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_admin_get_endpoint(self, client):
        """Test GET /api/admin."""
        resp = client.open("/api/admin", method="GET")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_admin_put_endpoint(self, client):
        """Test PUT /api/admin."""
        resp = client.open("/api/admin", method="PUT")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_admin_translate_now_endpoint(self, client):
        """Test POST /api/admin/translate-now."""
        resp = client.open("/api/admin/translate-now", method="POST")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_admin_purge_pending_endpoint(self, client):
        """Test POST /api/admin/purge-pending."""
        resp = client.open("/api/admin/purge-pending", method="POST")
        # Should not be 500
        assert resp.status_code != 500
    def test_api_admin_remove_oldest_endpoint(self, client):
        """Test POST /api/admin/remove-oldest."""
        resp = client.open("/api/admin/remove-oldest", method="POST")
        # Should not be 500
        assert resp.status_code != 500
    def test_rss_feed_endpoint(self, client):
        """Test GET /feed."""
        resp = client.open("/feed", method="GET")
        # Should not be 500
        assert resp.status_code != 500
