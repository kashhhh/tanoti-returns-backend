import base64
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.extensions import db
from app.models import AuthSession, Customer, GiftCardIssuance, Notification, Order, ReturnRequest, WebhookReceipt
from app.utils.sessions import cookie_name, digest
import test_workflows as fixtures


class RoundTwoTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    submit = fixtures.WorkflowTests.submit
    action = fixtures.WorkflowTests.action

    def test_cookie_login_has_no_bearer_and_production_attributes(self):
        self.app.config["SESSION_COOKIE_SECURE"] = True
        data = {"email": "staff@example.com", "secret": "test-admin-secret"}
        with patch("app.auth.admin_routes.send_admin_otp_email") as send:
            self.client.post("/api/admin/auth/request-otp", json=data)
        result = self.client.post("/api/admin/auth/verify-otp", json={**data, "otp": send.call_args.args[1]})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("token", result.json)
        cookie = result.headers["Set-Cookie"]
        for attribute in ("__Host-tanoti_admin_session=", "Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attribute, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertNotIn("Max-Age", cookie)
        raw = cookie.split(";", 1)[0].split("=", 1)[1]
        self.assertNotIn(raw, json.dumps(result.json))
        self.assertIsNotNone(db.session.get(AuthSession, digest(raw)))

    def test_csrf_required_bound_to_session_and_origin_checked(self):
        url = "/api/admin/settings"
        cookie_only = {"Cookie": self.admin_headers["Cookie"]}
        for headers in (cookie_only, {**cookie_only, "X-CSRF-Token": "wrong"},
                        {**cookie_only, "X-CSRF-Token": self.customer_headers["X-CSRF-Token"]},
                        {**self.admin_headers, "Origin": "https://evil.example"},
                        {**self.admin_headers, "Origin": "null"}):
            self.assertEqual(self.client.put(url, headers=headers, json={"return_window_days": 20}).status_code, 403)
        self.assertEqual(self.client.put(url, headers=self.admin_headers, json={"return_window_days": 20}).status_code, 200)
        self.assertEqual(self.client.get(url, headers=self.admin_headers).json["return_window_days"], 20)

    def test_login_csrf_and_non_object_json_rejected(self):
        with patch("app.auth.admin_routes.send_admin_otp_email") as send:
            result = self.client.post("/api/admin/auth/request-otp", json={"email": "staff@example.com", "secret": "test-admin-secret"}, headers={"Origin": "https://evil.example"})
            self.assertEqual(result.status_code, 403)
            self.assertEqual(self.client.post("/api/admin/auth/request-otp", data='{"email":"staff@example.com"}', content_type="text/plain").status_code, 400)
            self.assertEqual(self.client.put("/api/admin/settings", json=[], headers=self.admin_headers).status_code, 400)
            send.assert_not_called()

    def test_logout_revokes_copied_cookie(self):
        result = self.client.post("/api/auth/logout", json={}, headers=self.customer_headers)
        self.assertEqual(result.status_code, 200)
        self.assertIn("Max-Age=0", result.headers["Set-Cookie"])
        self.assertEqual(self.client.get("/api/customer/orders", headers=self.customer_headers).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/settings", headers=self.admin_headers).status_code, 200)

    def test_logout_all_only_revokes_same_role_and_identity(self):
        second = fixtures.session_headers("admin", "staff@example.com", "staff@example.com")
        self.client.post("/api/admin/auth/logout-all", json={}, headers=self.admin_headers)
        for headers in (self.admin_headers, second):
            self.assertEqual(self.client.get("/api/admin/settings", headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/api/customer/orders", headers=self.customer_headers).status_code, 200)

    def test_expired_session_and_changed_customer_email_rejected(self):
        customer = Customer.query.filter_by(email="customer@example.com").one()
        customer.email = "changed@example.com"
        db.session.commit()
        self.assertEqual(self.client.get("/api/customer/orders", headers=self.customer_headers).status_code, 401)
        row = AuthSession.query.filter_by(role="admin").one()
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(self.client.get("/api/admin/settings", headers=self.admin_headers).status_code, 401)

    def test_emergency_revocation_command(self):
        self.assertEqual(self.app.test_cli_runner().invoke(args=["revoke-sessions"]).exit_code, 0)
        self.assertEqual(self.client.get("/api/customer/orders", headers=self.customer_headers).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/settings", headers=self.admin_headers).status_code, 401)

    def test_settings_reject_invalid_financial_inputs(self):
        for data in ({"deduction_amount": "NaN"}, {"deduction_amount": "-1"},
                     {"deduction_amount": "0.001"}, {"deduction_amount": "Infinity"},
                     {"return_window_days": -1}, {"return_window_days": True}, {"deduction_enabled": "false"}):
            self.assertEqual(self.client.put("/api/admin/settings", json=data, headers=self.admin_headers).status_code, 400)

    def test_admin_audit_records_actor_and_is_not_customer_accessible(self):
        number = self.ready_refund()
        self.client.put("/api/admin/settings", json={"return_window_days": 20}, headers=self.admin_headers)
        response = self.client.get("/api/admin/audit", headers=self.admin_headers)
        self.assertEqual(response.status_code, 200)
        events = response.json["events"]
        self.assertTrue(any(e["action"] == "settings_updated" and e["actor"] == "staff@example.com" for e in events))
        self.assertTrue(any(e["target"] == number for e in events))
        self.assertEqual(self.client.get("/api/admin/audit", headers=self.customer_headers).status_code, 401)
        self.assertNotIn("test-admin-secret", response.get_data(as_text=True))

    def webhook(self, payload, topic="orders/updated", delivery="delivery-1", **headers):
        self.app.config.update(SHOPIFY_WEBHOOK_SECRET="test-webhook-secret", SHOPIFY_STORE_DOMAIN="test.myshopify.com")
        body = json.dumps(payload).encode()
        signature = base64.b64encode(hmac.new(b"test-webhook-secret", body, hashlib.sha256).digest()).decode()
        metadata = {"X-Shopify-Hmac-Sha256": signature, "X-Shopify-Shop-Domain": "test.myshopify.com",
                    "X-Shopify-Topic": topic, "X-Shopify-Webhook-Id": delivery, **headers}
        endpoint = {"orders/updated": "orders-updated", "orders/create": "orders-create", "fulfillments/create": "fulfillments-create"}[topic]
        return self.client.post("/api/webhooks/shopify/" + endpoint, data=body, content_type="application/json", headers=metadata)

    def order_payload(self, **changes):
        return {"id": 100, "order_number": "100", "updated_at": "2026-09-24T12:00:00Z",
                "customer": {"id": 100, "email": "customer@example.com"},
                "financial_status": "paid", **changes}

    def test_webhook_deduplicates_body_even_if_unsigned_id_changes(self):
        payload = self.order_payload()
        with patch("app.services.sync_service.upsert_order") as upsert:
            self.assertEqual(self.webhook(payload).status_code, 200)
            replay = self.webhook(payload, delivery="forged-new-delivery")
            self.assertTrue(replay.json["duplicate"])
            upsert.assert_called_once()
        self.assertEqual(WebhookReceipt.query.count(), 1)

    def test_webhook_failure_rolls_back_receipt_and_allows_retry(self):
        def fail(*args, **kwargs):
            db.session.add(Customer(email="must-rollback@example.com"))
            db.session.flush()
            raise RuntimeError("database/provider failure")
        with patch("app.services.sync_service.upsert_order", side_effect=fail):
            self.assertEqual(self.webhook(self.order_payload()).status_code, 503)
        self.assertEqual(WebhookReceipt.query.count(), 0)
        self.assertIsNone(Customer.query.filter_by(email="must-rollback@example.com").first())
        self.assertEqual(self.webhook(self.order_payload()).status_code, 200)
        self.assertEqual(WebhookReceipt.query.count(), 1)

    def test_stale_order_snapshot_cannot_undo_newer_update(self):
        self.assertEqual(self.webhook(self.order_payload(financial_status="refunded")).status_code, 200)
        self.assertEqual(self.webhook(self.order_payload(updated_at="2026-09-23T12:00:00Z")).status_code, 200)
        self.assertEqual(Order.query.filter_by(shopify_order_id="100").one().financial_status, "refunded")

    def test_webhook_store_topic_and_ids_validated(self):
        with patch("app.services.sync_service.upsert_order") as upsert:
            self.assertEqual(self.webhook(self.order_payload(), **{"X-Shopify-Shop-Domain": "wrong.myshopify.com"}).status_code, 401)
            self.assertEqual(self.webhook(self.order_payload(), **{"X-Shopify-Topic": "products/update"}).status_code, 400)
            self.assertEqual(self.webhook({"order_id": "../customers"}, topic="fulfillments/create").status_code, 400)
            upsert.assert_not_called()

    def test_fulfillment_lookup_failure_is_retryable(self):
        with patch("app.services.shopify_client.fetch_order", side_effect=RuntimeError("Shopify unavailable")):
            self.assertEqual(self.webhook({"order_id": 100}, topic="fulfillments/create").status_code, 503)
        self.assertEqual(WebhookReceipt.query.count(), 0)

    def ready_refund(self):
        number = self.submit().json["return_number"]
        self.action(number, "accept-photos")
        self.action(number, "mark-parcel-received")
        return number

    def test_gift_timeout_fences_retries_and_rejection(self):
        number = self.ready_refund()
        with patch("app.services.shopify_client.issue_gift_card", side_effect=TimeoutError("provider may have accepted")) as create:
            self.assertEqual(self.action(number, "accept-parcel").status_code, 409)
            self.assertEqual(self.action(number, "accept-parcel").status_code, 409)
            self.assertEqual(self.action(number, "reject-parcel", rejection_reason="tags_removed").status_code, 409)
            create.assert_called_once()
        attempt = GiftCardIssuance.query.one()
        self.assertEqual(attempt.state, "uncertain")
        self.assertEqual(len(attempt.code), 20)
        rows = self.client.get("/api/admin/requests", headers=self.admin_headers)
        self.assertNotIn(attempt.code, rows.get_data(as_text=True))

    def test_gift_reconcile_checks_provider_and_notifies_once(self):
        number = self.ready_refund()
        with patch("app.services.shopify_client.issue_gift_card", side_effect=TimeoutError()):
            self.action(number, "accept-parcel")
        attempt = GiftCardIssuance.query.one()
        card = {"id": 567, "note": "Refund for " + number, "initial_value": str(attempt.amount),
                "currency": "INR", "last_characters": attempt.code[-4:], "disabled_at": None}
        with patch("app.services.shopify_client.fetch_gift_card", return_value={**card, "initial_value": "1.00"}):
            self.assertEqual(self.action(number, "reconcile-gift-card", gift_card_id="567").status_code, 409)
        with patch("app.services.shopify_client.fetch_gift_card", return_value=card) as fetch, \
                patch("app.services.shopify_client.issue_gift_card") as create:
            self.assertEqual(self.action(number, "reconcile-gift-card", gift_card_id="567").status_code, 200)
            self.assertEqual(self.action(number, "reconcile-gift-card", gift_card_id="567").status_code, 200)
            fetch.assert_called_once()
            create.assert_not_called()
        self.assertEqual(GiftCardIssuance.query.one().state, "confirmed")
        self.assertEqual(Notification.query.filter_by(event_key=number+":gift_card").count(), 1)

    def test_crash_after_provider_success_preserves_fence(self):
        number = self.ready_refund()
        def created(**kw):
            return {"id": 567, "initial_value": kw["amount"], "currency": "INR", "note": kw["note"], "code": kw["code"]}
        with patch("app.services.shopify_client.issue_gift_card", side_effect=created) as create, \
                patch("app.admin.routes.queue_update", side_effect=RuntimeError("crash before local final commit")):
            with self.assertRaises(RuntimeError):
                self.action(number, "accept-parcel")
            db.session.rollback()
            self.assertEqual(self.action(number, "accept-parcel").status_code, 409)
            create.assert_called_once()
        self.assertEqual(GiftCardIssuance.query.one().state, "pending")

    def test_legacy_ambiguous_refund_is_not_reissued(self):
        number = self.ready_refund()
        req = ReturnRequest.query.filter_by(return_number=number).one()
        req.parcel_decision_at = datetime.utcnow()
        db.session.commit()
        with patch("app.services.shopify_client.issue_gift_card") as create:
            self.assertEqual(self.action(number, "accept-parcel").status_code, 409)
            create.assert_not_called()
