import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import test_workflows as fixtures
import test_customer_improvements as customer_fixtures
from app.extensions import db
from app.models import AuthSession, OTPToken, Order, OrderItem, PaymentMethod
from app.services import whatsapp_service


class AddressPhoneLoginTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown

    def test_normalizes_only_unambiguous_address_numbers(self):
        for value in ("98765 43210", "919876543210", "+91 (98765) 43210"):
            self.assertEqual(whatsapp_service.address_phone({}, {
                "shipping_address": {"phone": value, "country_code": "IN"}}), "+919876543210")
        for address in ({"phone": "9876543210"}, {"phone": "9876543210", "country": "USA"},
                        {"phone": "1234567890", "country": "India"}, {"phone": []}, None):
            self.assertIsNone(whatsapp_service.address_phone({}, {"shipping_address": address}))
        self.assertEqual(whatsapp_service.address_phone({}, {"shipping_address": {
            "phone": "+12025550123", "country_code": "US"}}), "+12025550123")

    def test_default_address_fallback_resend_and_order_access(self):
        order = customer_fixtures.CustomerImprovementsTests.login_order_payload(self)
        account = {**order["customer"], "default_address": {
            "phone": "9876543210", "country": "India"}}
        with patch("app.auth.routes.shopify_client.find_order_for_login", return_value=order), \
                patch("app.auth.routes.shopify_client.find_customer_by_email", return_value=account), \
                patch("app.auth.routes.whatsapp_service.send_otp_whatsapp") as send:
            options = self.client.post("/api/auth/delivery-options", json={"order_id": "100"})
            self.assertEqual(options.json, {"channels": ["email", "whatsapp"]})
            send.assert_not_called()
            first = self.client.post("/api/auth/request-otp", json={
                "order_id": "100", "channel": "whatsapp", "phone": "+12025550123"})
            self.assertEqual(send.call_args.args[0], "+919876543210")
            scope = db.session.get(OrderItem, self.item_id).order_id
            self.assertEqual(OTPToken.query.one().order_scope_id, scope)
            OTPToken.query.one().created_at -= timedelta(seconds=31)
            db.session.commit()
            second = self.client.post("/api/auth/resend-otp", json={"order_id": "100", "channel": "whatsapp"})
            self.assertEqual(second.status_code, 200)
            code = send.call_args.args[1]
            self.assertEqual(self.client.post("/api/auth/verify-otp", json={
                "challenge": first.json["challenge"], "otp": code}).status_code, 400)
            with patch("app.auth.routes.sync_service.sync_orders_for_customer") as sync_all:
                login = self.client.post("/api/auth/verify-otp", json={
                    "challenge": second.json["challenge"], "otp": code, "order_scope_id": None})
                self.assertEqual(login.status_code, 200)
                sync_all.assert_not_called()
        headers = {"Cookie": login.headers["Set-Cookie"].split(";", 1)[0],
                   "X-CSRF-Token": login.json["csrf_token"]}
        scoped_session = AuthSession.query.filter_by(order_scope_id=scope).one()
        self.assertEqual(scoped_session.subject, str(db.session.get(Order, scope).customer_id))
        other = Order(shopify_order_id="200", order_number="200", customer_id=int(scoped_session.subject),
                      payment_method=PaymentMethod.PREPAID)
        other_item = OrderItem(order=other, shopify_line_item_id="2", product_title="Other secret item",
                               price=999, delivered_at=datetime.utcnow())
        db.session.add(other_item)
        db.session.commit()
        data = self.client.get("/api/customer/orders", headers=headers).json
        self.assertEqual([item["id"] for item in data["eligible"] + data["expired"]], [self.item_id])
        self.assertEqual(self.client.get("/api/customer/orders/search?order_number=200", headers=headers).status_code, 404)
        self.assertEqual(self.client.get(f"/api/customer/order-items/{other_item.id}", headers=headers).status_code, 404)
        for kind in ("return", "exchange"):
            self.assertEqual(self.client.post(f"/api/customer/order-items/{other_item.id}/{kind}",
                                             headers=headers).status_code, 404)
        self.assertEqual(self.client.get("/api/customer/orders/search?order_number=100", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/customer/my-requests", headers=headers).json, {"requests": []})
        # Requests on another order stay private; this order remains actionable.
        original_item_id = self.item_id
        self.item_id = other_item.id
        with patch("app.customer.routes.sync_one"), patch("app.customer.routes.deliver_pending"):
            self.assertEqual(fixtures.WorkflowTests.submit(self).status_code, 201)
            self.item_id = original_item_id
            original_headers = self.customer_headers
            self.customer_headers = headers
            self.assertEqual(fixtures.WorkflowTests.submit(self).status_code, 201)
            self.customer_headers = original_headers
        history = self.client.get("/api/customer/my-requests", headers=headers).json["requests"]
        self.assertEqual(len(history), 1)
        self.assertNotEqual(history[0]["item"], "Other secret item")
        with patch("app.customer.routes.sync_service.sync_orders_for_customer") as sync_all, \
                patch("app.customer.routes.shopify_client.fetch_order", return_value=order), \
                patch("app.customer.routes.sync_service.upsert_order") as sync_one:
            self.assertEqual(self.client.post("/api/customer/resync", headers=headers).status_code, 200)
            sync_one.assert_called_once_with(order)
            sync_all.assert_not_called()
        # A restricted user cannot revoke the account owner's other sessions.
        self.assertEqual(self.client.post("/api/auth/logout-all", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/customer/orders", headers=self.customer_headers).status_code, 200)

    def test_shipping_fallback_and_account_phone_priority(self):
        order = customer_fixtures.CustomerImprovementsTests.login_order_payload(self)
        order["shipping_address"] = {"phone": "9876543210", "country_code": "IN"}
        for phone, expected_scope, expected_phone in ((None, True, "+919876543210"),
                                                     ("+12025550123", False, "+12025550123")):
            OTPToken.query.delete()
            db.session.commit()
            account = {**order["customer"], "phone": phone}
            order["customer"] = account
            with patch("app.auth.routes.shopify_client.find_order_for_login", return_value=order), \
                    patch("app.auth.routes.shopify_client.find_customer_by_email", return_value=account), \
                    patch("app.auth.routes.whatsapp_service.send_otp_whatsapp") as send:
                response = self.client.post("/api/auth/request-otp", json={"order_id": "100", "channel": "whatsapp"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(send.call_args.args[0], expected_phone)
            self.assertEqual(OTPToken.query.one().order_scope_id is not None, expected_scope)

    def test_email_login_does_not_enable_account_access_via_address(self):
        account = {"id": "10", "email": "customer@example.com", "default_address": {
            "phone": "9876543210", "country": "India"}}
        with patch("app.auth.routes.shopify_client.find_customer_by_email", return_value=account), \
                patch("app.auth.routes.whatsapp_service.send_otp_whatsapp") as send:
            options = self.client.post("/api/auth/delivery-options", json={"email": account["email"]})
            self.assertEqual(options.json, {"channels": ["email"]})
            self.client.post("/api/auth/request-otp", json={"email": account["email"], "channel": "whatsapp"})
            send.assert_not_called()
            self.assertEqual(OTPToken.query.count(), 0)


    def test_diagnostic_reports_contact_source_without_sending_or_exposing_contacts(self):
        import json
        order = customer_fixtures.CustomerImprovementsTests.login_order_payload(self)
        account = {**order["customer"], "default_address": {"phone": "9876543210", "country": "India"}}
        with patch("app.services.shopify_client.find_customer_by_email", return_value=account), \
                patch("app.services.shopify_client.fetch_orders_for_customer", return_value=[order]), \
                patch("app.services.whatsapp_service.send_otp_whatsapp") as send:
            result = self.app.test_cli_runner().invoke(args=["diagnose-customer-login", "--email", account["email"]])
        self.assertEqual(result.exit_code, 0, result.output)
        report = json.loads(result.output)
        self.assertTrue(report["scope_migration_applied"])
        self.assertTrue(report["default_address_phone_usable"])
        self.assertEqual(report["email_login_channels"], ["email"])
        self.assertEqual(report["orders"][0]["phone_source"], "address")
        self.assertNotIn("9876543210", result.output)
        self.assertNotIn(account["email"], result.output)
        send.assert_not_called()
        self.assertEqual(OTPToken.query.count(), 0)

    def test_diagnostic_reports_missing_configuration_and_lookup_errors_safely(self):
        import json
        self.app.config.update(TESTING_MODE=False, WHATSAPP_ACCESS_TOKEN=None)
        with patch("app.services.shopify_client.find_customer_by_email", side_effect=RuntimeError("secret details")):
            result = self.app.test_cli_runner().invoke(args=["diagnose-customer-login", "--email", "customer@example.com"])
        self.assertEqual(result.exit_code, 0, result.output)
        report = json.loads(result.output)
        self.assertFalse(report["whatsapp_configured"])
        self.assertIn("WHATSAPP_ACCESS_TOKEN", report["missing_whatsapp_settings"])
        self.assertEqual(report["shopify_lookup_error"], "RuntimeError")
        self.assertNotIn("secret details", result.output)


if __name__ == "__main__":
    unittest.main()
