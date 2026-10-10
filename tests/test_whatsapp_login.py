import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import test_workflows as fixtures
import test_customer_improvements as customer_fixtures
from app.extensions import db
from app.models import OTPToken
from app.services import whatsapp_service


class WhatsAppLoginTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown

    def issue(self, channel="whatsapp", customer=None, route="request-otp", **extra):
        customer = customer or {"id": "10", "email": "customer@example.com", "phone": "+919876543210"}
        with patch("app.auth.routes.shopify_client.find_customer_by_email", return_value=customer), \
                patch("app.auth.routes.whatsapp_service.send_otp_whatsapp") as whatsapp, \
                patch("app.auth.routes.email_service.send_otp_email") as email:
            response = self.client.post("/api/auth/" + route, json={
                "email": "customer@example.com", "channel": channel, **extra,
            })
        return response, whatsapp, email

    def test_registered_number_only_and_single_use_login(self):
        response, whatsapp, email = self.issue(phone="+12025550123")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(whatsapp.call_args.args[0], "+919876543210")
        email.assert_not_called()
        self.assertNotIn("9876543210", response.get_data(as_text=True))
        payload = {"challenge": response.json["challenge"], "otp": whatsapp.call_args.args[1]}
        self.assertEqual(self.client.post("/api/auth/verify-otp", json=payload).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/verify-otp", json=payload).status_code, 400)

    def test_missing_phone_is_private_and_no_shipping_fallback(self):
        response, whatsapp, email = self.issue(customer={"id": "10", "email": "customer@example.com",
            "default_address": {"phone": "+919876543210"}})
        self.assertEqual(response.status_code, 200)
        whatsapp.assert_not_called()
        email.assert_not_called()
        self.assertEqual(OTPToken.query.count(), 0)

    def test_order_login_uses_customer_phone_not_order_contact(self):
        order = customer_fixtures.CustomerImprovementsTests.login_order_payload(self)
        order["customer"]["phone"] = "+919876543210"
        order["phone"] = "+12025550123"
        with patch("app.auth.routes.shopify_client.find_order_for_login", return_value=order), \
                patch("app.auth.routes.whatsapp_service.send_otp_whatsapp") as send:
            response = self.client.post("/api/auth/request-otp", json={"order_id": "100", "channel": "whatsapp"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(send.call_args.args[0], "+919876543210")

    def test_unknown_customer_and_missing_phone_have_same_response(self):
        missing, _, _ = self.issue(customer={"id": "10", "email": "customer@example.com"})
        with patch("app.auth.routes.shopify_client.find_customer_by_email", return_value=None):
            unknown = self.client.post("/api/auth/request-otp", json={"email": "unknown@example.com", "channel": "whatsapp"})
        self.assertEqual(unknown.status_code, missing.status_code)
        self.assertEqual({k:v for k,v in unknown.json.items() if k != "challenge"},
                         {k:v for k,v in missing.json.items() if k != "challenge"})

    def test_channels_share_cooldown_and_invalidate_superseded_codes(self):
        first, whatsapp, _ = self.issue()
        self.assertEqual(self.issue("email")[0].status_code, 429)
        self.assertEqual(self.issue(route="resend-otp")[0].status_code, 429)
        OTPToken.query.one().created_at -= timedelta(seconds=31)
        db.session.commit()
        second, _, email = self.issue("email", route="resend-otp")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.client.post("/api/auth/verify-otp", json={
            "challenge": first.json["challenge"], "otp": whatsapp.call_args.args[1],
        }).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/verify-otp", json={
            "challenge": second.json["challenge"], "otp": email.call_args.args[1],
        }).status_code, 200)

    def test_invalid_channels(self):
        for channel in (None, [], {}, "sms"):
            self.assertEqual(self.issue(channel)[0].status_code, 400)

    def test_unconfigured_provider_leaves_email_available(self):
        self.app.config.update(TESTING_MODE=False, WHATSAPP_ACCESS_TOKEN=None)
        self.assertEqual(self.issue()[0].status_code, 503)
        self.assertEqual(self.issue("email")[0].status_code, 200)

    def test_failed_delivery_consumes_code(self):
        with patch("app.auth.routes.shopify_client.find_customer_by_email", return_value={
                "id": "10", "email": "customer@example.com", "phone": "+919876543210"}), \
                patch("app.auth.routes.whatsapp_service.send_otp_whatsapp", side_effect=RuntimeError("provider down")):
            result = self.client.post("/api/auth/request-otp", json={"email": "customer@example.com", "channel": "whatsapp"})
        self.assertEqual(result.status_code, 503)
        self.assertTrue(OTPToken.query.one().consumed)

    def test_provider_payload_and_error_handling(self):
        self.app.config.update(TESTING_MODE=False, WHATSAPP_ACCESS_TOKEN="test-token",
            WHATSAPP_PHONE_NUMBER_ID="123", WHATSAPP_OTP_TEMPLATE_NAME="tanoti_returns_login")
        response = Mock(ok=True)
        response.json.return_value = {"messages": [{"id": "test-message"}]}
        with patch.object(whatsapp_service.requests, "post", return_value=response) as send:
            whatsapp_service.send_otp_whatsapp("+919876543210", "123456")
            payload = send.call_args.kwargs["json"]
            self.assertEqual(payload["to"], "919876543210")
            self.assertEqual(payload["template"]["name"], "tanoti_returns_login")
            for component in payload["template"]["components"]:
                self.assertEqual(component["parameters"][0]["text"], "123456")
            response.ok = False
            with self.assertRaises(RuntimeError):
                whatsapp_service.send_otp_whatsapp("+919876543210", "123456")

    def test_console_delivery_has_no_network(self):
        with patch("builtins.print") as output:
            self.assertEqual(whatsapp_service.send_otp_whatsapp("+919876543210", "123456"), "console")
        output.assert_called_once()

    def test_phone_normalization(self):
        self.assertEqual(whatsapp_service.registered_phone({"phone": "+91 (98765) 43210"}), "+919876543210")
        for value in (None, "9876543210", "+0123456789", "invalid", 123):
            self.assertIsNone(whatsapp_service.registered_phone({"phone": value}))

    def test_delivery_options_require_registered_phone_and_send_no_code(self):
        for phone, channels in ((None, ["email"]), ("", ["email"]),
                                ("invalid", ["email"]), ("+919876543210", ["email", "whatsapp"])):
            with self.subTest(phone=phone):
                response, whatsapp, email = self.issue(route="delivery-options", customer={
                    "id": "10", "email": "customer@example.com", "phone": phone,
                    "default_address": {"phone": "+919876543210"},
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json, {"channels": channels})
                whatsapp.assert_not_called()
                email.assert_not_called()
        self.assertEqual(OTPToken.query.count(), 0)

    def test_options_unknown_account_and_unconfigured_provider_are_email_only(self):
        with patch("app.auth.routes.shopify_client.find_customer_by_email", return_value=None):
            result = self.client.post("/api/auth/delivery-options", json={"email": "missing@example.com"})
        self.assertEqual(result.json, {"channels": ["email"]})
        self.app.config.update(TESTING_MODE=False, WHATSAPP_ACCESS_TOKEN=None)
        result, _, _ = self.issue(channel="email", route="delivery-options")
        self.assertEqual(result.json, {"channels": ["email"]})

    def test_options_do_not_use_send_budget_or_invalidate_existing_otp(self):
        first, _, _ = self.issue()
        for _ in range(5):
            self.assertEqual(self.issue(route="delivery-options")[0].status_code, 200)
        self.assertFalse(OTPToken.query.one().consumed)
        self.assertEqual(OTPToken.query.one().challenge, first.json["challenge"])
        self.assertEqual(self.issue(route="delivery-options")[0].status_code, 429)

    def test_order_delivery_options_match_phone_and_eligibility(self):
        order = customer_fixtures.CustomerImprovementsTests.login_order_payload(self)
        for phone in (None, "+919876543210"):
            order["customer"]["phone"] = phone
            with patch("app.auth.routes.shopify_client.find_order_for_login", return_value=order), \
                    patch("app.auth.routes.shopify_client.find_customer_by_email", return_value=order["customer"]):
                result = self.client.post("/api/auth/delivery-options", json={"order_id": "100"})
            self.assertEqual(result.json["channels"], ["email", "whatsapp"] if phone else ["email"])
        order["fulfillments"] = []
        with patch("app.auth.routes.shopify_client.find_order_for_login", return_value=order):
            result = self.client.post("/api/auth/delivery-options", json={"order_id": "100"})
        self.assertEqual(result.json, {"channels": ["email"]})
        self.assertEqual(OTPToken.query.count(), 0)

    def test_order_whatsapp_resolves_account_phone_for_options_send_resend_and_verify(self):
        order = customer_fixtures.CustomerImprovementsTests.login_order_payload(self)
        order["phone"] = "+12025550123"
        account = {**order["customer"], "phone": "+919876543210"}
        with patch("app.auth.routes.shopify_client.find_order_for_login", return_value=order), \
                patch("app.auth.routes.shopify_client.find_customer_by_email", return_value=account), \
                patch("app.auth.routes.whatsapp_service.send_otp_whatsapp") as send, \
                patch("app.auth.routes.email_service.send_otp_email") as email:
            options = self.client.post("/api/auth/delivery-options", json={"order_id": "#100"})
            self.assertEqual(options.json, {"channels": ["email", "whatsapp"]})
            send.assert_not_called()
            self.assertEqual(OTPToken.query.count(), 0)
            first = self.client.post("/api/auth/request-otp", json={"order_id": "#100", "channel": "whatsapp"})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(send.call_args.args[0], account["phone"])
            self.assertEqual(self.client.post("/api/auth/resend-otp", json={"order_id": "100", "channel": "whatsapp"}).status_code, 429)
            OTPToken.query.one().created_at -= timedelta(seconds=31)
            db.session.commit()
            resend = self.client.post("/api/auth/resend-otp", json={"order_id": "100", "channel": "whatsapp"})
            self.assertEqual(resend.status_code, 200)
            email.assert_not_called()
            self.assertEqual(send.call_args.args[0], account["phone"])
            code = send.call_args.args[1]
            self.assertEqual(self.client.post("/api/auth/verify-otp", json={"challenge": first.json["challenge"], "otp": code}).status_code, 400)
            self.assertEqual(self.client.post("/api/auth/verify-otp", json={"challenge": resend.json["challenge"], "otp": code}).status_code, 200)
            self.assertEqual(self.client.post("/api/auth/verify-otp", json={"challenge": resend.json["challenge"], "otp": code}).status_code, 400)

    def test_order_contact_lookup_cannot_redirect_to_another_customer(self):
        order = customer_fixtures.CustomerImprovementsTests.login_order_payload(self)
        for account in (None, {"id": "999", "email": order["email"], "phone": "+919876543210"},
                        {"id": "10", "email": "other@example.com", "phone": "+919876543210"}):
            with self.subTest(account=account), \
                    patch("app.auth.routes.shopify_client.find_order_for_login", return_value=order), \
                    patch("app.auth.routes.shopify_client.find_customer_by_email", return_value=account), \
                    patch("app.auth.routes.whatsapp_service.send_otp_whatsapp") as send:
                options = self.client.post("/api/auth/delivery-options", json={"order_id": "100"})
                self.assertEqual(options.json, {"channels": ["email"]})
                result = self.client.post("/api/auth/request-otp", json={"order_id": "100", "channel": "whatsapp"})
                self.assertEqual(result.status_code, 200)
                send.assert_not_called()
                self.assertEqual(OTPToken.query.count(), 0)

    def test_options_lookup_errors_and_invalid_input(self):
        for payload in ({}, {"email": "bad"}, {"order_id": []}, {"email": "customer@example.com", "order_id": "100"}):
            self.assertEqual(self.client.post("/api/auth/delivery-options", json=payload).status_code, 400)
        with patch("app.auth.routes.shopify_client.find_customer_by_email", side_effect=RuntimeError("unavailable")):
            result = self.client.post("/api/auth/delivery-options", json={"email": "customer@example.com"})
        self.assertEqual(result.status_code, 503)
