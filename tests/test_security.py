import hashlib
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import jwt
from sqlalchemy import select
from app.extensions import db
from app.models import OTPToken, ReturnRequest, SecurityRateLimit
from app.security import validate_config
from app.services.shopify_client import find_customer_by_email
import test_workflows as fixtures


class SecurityTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown

    def send_code(self):
        with patch("app.auth.routes.shopify_client.find_customer_by_email", return_value={"id": "100"}), \
                patch("app.auth.routes.email_service.send_otp_email") as send:
            result = self.client.post("/api/auth/request-otp", json={"email": "customer@example.com"})
        self.assertEqual(result.status_code, 200)
        return send.call_args.args[1]

    def verify(self, code):
        return self.client.post("/api/auth/verify-otp", json={"email": "customer@example.com", "otp": code})

    def test_both_send_routes_share_cooldown(self):
        self.send_code()
        with patch("app.auth.routes.shopify_client.find_customer_by_email") as lookup:
            for route in ("request-otp", "resend-otp"):
                self.assertEqual(self.client.post("/api/auth/" + route, json={"email": "customer@example.com"}).status_code, 429)
            lookup.assert_not_called()

    def test_customer_code_is_keyed_single_use_and_old_codes_stay_invalid(self):
        old = self.send_code()
        token = OTPToken.query.first()
        self.assertNotEqual(token.otp_hash, hashlib.sha256(old.encode()).hexdigest())
        token.created_at -= timedelta(minutes=1)
        db.session.commit()
        with patch("app.auth.routes._generate_otp", return_value="654321" if old != "654321" else "123456"):
            new = self.send_code()
        self.assertEqual(self.verify(old).status_code, 400)
        self.assertEqual(self.verify(new).status_code, 200)
        self.assertEqual(self.verify(new).status_code, 400)
        self.assertEqual(self.verify(old).status_code, 400)

    def test_customer_attempt_limit_and_expiry(self):
        code = self.send_code()
        for _ in range(5):
            self.assertEqual(self.verify("000000" if code != "000000" else "111111").status_code, 400)
        self.assertEqual(self.verify(code).status_code, 429)
        token = OTPToken.query.first()
        token.attempts = 0
        token.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(self.verify(code).status_code, 400)

    def test_unknown_email_has_same_send_response(self):
        with patch("app.auth.routes.shopify_client.find_customer_by_email", return_value=None):
            missing = self.client.post("/api/auth/request-otp", json={"email": "missing@example.com"})
        with patch("app.auth.routes.shopify_client.find_customer_by_email", return_value={"id": "100"}), \
                patch("app.auth.routes.email_service.send_otp_email"):
            existing = self.client.post("/api/auth/request-otp", json={"email": "customer@example.com"})
        self.assertEqual((missing.status_code, missing.json), (existing.status_code, existing.json))

    def test_bad_login_inputs_and_query_injection(self):
        with patch("app.auth.routes.shopify_client.find_customer_by_email") as lookup:
            for value in ([], "text", {"email": []}, {"email": "victim@example.com OR id:1"}, {"email": "*"}):
                self.assertEqual(self.client.post("/api/auth/request-otp", json=value).status_code, 400)
            lookup.assert_not_called()

    def test_shopify_result_must_match_email(self):
        response = Mock()
        response.json.return_value = {"customers": [{"email": "victim@example.com", "id": "1"}, {"email": "Customer@Example.com", "id": "2"}]}
        with patch("app.services.shopify_client._headers", return_value={}), patch("requests.get", return_value=response):
            self.assertEqual(find_customer_by_email("customer@example.com")["id"], "2")
            self.assertIsNone(find_customer_by_email("unknown@example.com"))

    def test_auth_limit_shared_across_routes_and_ignores_forwarded_ip(self):
        for _ in range(30):
            self.assertEqual(self.client.post("/api/admin/auth/verify-otp", json={}).status_code, 401)
        response = self.client.post("/api/auth/request-otp", json={}, headers={"X-Forwarded-For": "203.0.113.4"})
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)
        self.assertTrue(all("@" not in row.key for row in db.session.scalars(select(SecurityRateLimit))))

    def test_photos_require_staff_auth_and_existing_record(self):
        self.assertEqual(fixtures.WorkflowTests.submit(self).status_code, 201)
        path = ReturnRequest.query.first().photo_urls[0]
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers=self.customer_headers).status_code, 401)
        response = self.client.get(path, headers=self.admin_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response.close()
        self.assertEqual(self.client.get("/uploads/../.env", headers=self.admin_headers).status_code, 404)
        self.assertEqual(self.client.get(path.replace(".jpg", "0.jpg"), headers=self.admin_headers).status_code, 404)

    def test_customer_jwt_requires_expiry_audience_and_role(self):
        base = {"sub": "1", "role": "customer", "aud": "tanoti-customer", "iat": datetime.utcnow(),
                "customer_id": 1, "exp": datetime.utcnow() + timedelta(hours=1)}
        for key in ("exp", "iat", "sub", "role", "aud"):
            payload = {k: v for k, v in base.items() if k != key}
            token = jwt.encode(payload, self.app.config["SECRET_KEY"], algorithm="HS256")
            self.assertEqual(self.client.get("/api/customer/orders", headers={"Authorization": "Bearer " + token}).status_code, 401)
        self.assertEqual(self.client.get("/api/customer/orders", headers=self.admin_headers).status_code, 401)

    def test_security_headers_even_on_auth_errors(self):
        response = self.client.get("/api/customer/orders", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_production_startup_fails_closed(self):
        self.app.config.update(TESTING=False, APP_ENV="production", SECRET_KEY="x" * 48,
                               ADMIN_SECRET_KEY="y" * 48, RESEND_API_KEY="configured", TESTING_MODE=False,
                               SHOPIFY_WEBHOOK_SECRET="configured", CORS_ORIGINS="https://returns.example.com", SESSION_COOKIE_SECURE=True)
        validate_config(self.app)
        for key, bad_value in (("SECRET_KEY", "change-me-in-prod"), ("ADMIN_SECRET_KEY", "x" * 48),
                               ("TESTING_MODE", True), ("DEBUG", True), ("CORS_ORIGINS", "*"),
                               ("RESEND_API_KEY", ""), ("RATE_LIMIT_ENABLED", False)):
            old = self.app.config[key]
            self.app.config[key] = bad_value
            with self.assertRaises(RuntimeError, msg=key):
                validate_config(self.app)
            self.app.config[key] = old
