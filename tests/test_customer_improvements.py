import unittest
from unittest.mock import patch, Mock
from datetime import timedelta

import test_workflows as fixtures
from app.extensions import db
from app.models import OTPToken, OrderItem, ReturnRequest, GiftCardIssuance, Notification
from app.services import shopify_client, sync_service, shopify_returns


class CustomerImprovementsTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    submit = fixtures.WorkflowTests.submit
    action = fixtures.WorkflowTests.action

    def order_login(self):
        order = {"email": "customer@example.com", "customer": {"id": "10", "email": "customer@example.com"}}
        with patch.object(shopify_client, "find_order_for_login", return_value=order), patch("app.auth.routes.email_service.send_otp_email") as send:
            result = self.client.post("/api/auth/request-otp", json={"order_id": "#100"})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("customer@example.com", result.get_data(as_text=True))
        self.assertEqual(send.call_args.args[0], "customer@example.com")
        return result.json["challenge"], send.call_args.args[1]

    def test_order_login_is_private_single_use_and_requires_code(self):
        challenge, code = self.order_login()
        self.assertEqual(self.client.post("/api/auth/verify-otp", json={"challenge": challenge, "otp": "invalid"}).status_code, 400)
        response = self.client.post("/api/auth/verify-otp", json={"challenge": challenge, "otp": code})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["customer"]["email"], "customer@example.com")
        self.assertEqual(self.client.post("/api/auth/verify-otp", json={"challenge": challenge, "otp": code}).status_code, 400)

    def test_unknown_order_has_same_response_shape_and_no_email(self):
        challenge, _ = self.order_login()
        with patch.object(shopify_client, "find_order_for_login", return_value=None), patch("app.auth.routes.email_service.send_otp_email") as send:
            result = self.client.post("/api/auth/request-otp", json={"order_id": "999"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json["challenge"]), len(challenge))
        send.assert_not_called()
        self.assertEqual(self.client.post("/api/auth/verify-otp", json={"challenge": result.json["challenge"], "otp": "123456"}).status_code, 400)

    def test_order_contact_cannot_access_another_customer_account(self):
        order = {"email": "other@example.com", "customer": {"id": "10", "email": "customer@example.com"}}
        with patch.object(shopify_client, "find_order_for_login", return_value=order), patch("app.auth.routes.email_service.send_otp_email") as send:
            self.assertEqual(self.client.post("/api/auth/request-otp", json={"order_id": "100"}).status_code, 200)
        send.assert_not_called()

    def test_expired_challenge_cannot_log_in(self):
        challenge, code = self.order_login()
        OTPToken.query.one().expires_at -= timedelta(hours=1)
        db.session.commit()
        self.assertEqual(self.client.post("/api/auth/verify-otp", json={"challenge": challenge, "otp": code}).status_code, 400)

    def test_lookup_rejects_fuzzy_order_match(self):
        response = Mock(status_code=404)
        response.json.return_value = {"orders": [{"id": 12, "name": "#1001", "order_number": 1001}]}
        with patch.object(shopify_client, "_headers", return_value={}), patch.object(shopify_client.requests, "get", return_value=response):
            self.assertIsNone(shopify_client.find_order_for_login("100"))

    def test_variant_images_and_default_fallback(self):
        product = {"image": {"src": "default"}, "images": [{"id": 1, "src": "red", "variant_ids": [10]}, {"id": 2, "src": "blue"}],
                   "variants": [{"id": 20, "image_id": 2}]}
        response = Mock(ok=True)
        response.json.return_value = {"product": product}
        with patch.object(shopify_client, "_headers", return_value={}), patch.object(shopify_client.requests, "get", return_value=response):
            self.assertEqual(shopify_client.get_product_image_url("1", "10"), "red")
            self.assertEqual(shopify_client.get_product_image_url("1", "20"), "blue")
            self.assertEqual(shopify_client.get_product_image_url("1", "30"), "default")

    def test_existing_images_refresh_and_cache_by_variant(self):
        item = db.session.get(OrderItem, self.item_id)
        item.shopify_product_id, item.shopify_variant_id = "1", "10"
        other = OrderItem(shopify_product_id="1", shopify_variant_id="20")
        with patch.object(shopify_client, "get_product_image_url", side_effect=["red", "blue"]) as fetch:
            sync_service.refresh_item_images([item, other, item])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual((item.image_url, other.image_url), ("red", "blue"))

    def test_optional_shopify_sync_does_not_call_provider(self):
        number = self.submit().json["return_number"]
        with patch.object(shopify_returns, "graphql") as read, patch.object(shopify_returns, "mutation") as mutate:
            shopify_returns.sync_one(ReturnRequest.query.one(), "return")
            shopify_returns.retry_pending()
        read.assert_not_called()
        mutate.assert_not_called()
        self.assertEqual(self.action(number, "sync-shopify").status_code, 410)

    def test_manual_shipment_sends_only_when_arranged_once(self):
        number = self.submit().json["return_number"]
        self.action(number, "accept-photos")
        self.assertEqual(Notification.query.count(), 1)
        payload = dict(leg="pickup", carrier="delhivery", tracking_id="TRACK1")
        self.assertEqual(self.action(number, "confirm-shipment", **payload).status_code, 200)
        self.assertEqual(self.action(number, "confirm-shipment", **payload).status_code, 200)
        self.assertEqual(Notification.query.count(), 2)
        self.assertIn("TRACK1", Notification.query.filter_by(event_key=number+":pickup_booked").one().html)

    def test_currency_mismatch_prevents_financial_mutation(self):
        info = {"shop": {"currencyCode": "USD"}, "currentAppInstallation": {"accessScopes": [{"handle": "write_gift_cards"}]}}
        with patch.object(shopify_client, "graphql", return_value=info), patch.object(shopify_client, "mutation") as create:
            with self.assertRaisesRegex(shopify_client.ShopifyUserError, "currency is USD"):
                shopify_client.issue_gift_card("999", code="ABC")
        create.assert_not_called()

    def test_graphql_uses_provider_code_and_verified_currency(self):
        info = {"shop": {"currencyCode": "INR"}, "currentAppInstallation": {"accessScopes": [{"handle": "write_gift_cards"}]}}
        card = {"id": "gid://shopify/GiftCard/123", "note": "Refund", "initialValue": {"amount": "999.00", "currencyCode": "INR"}, "lastCharacters": "ABCD", "enabled": True}
        with patch.object(shopify_client, "graphql", return_value=info), patch.object(shopify_client, "mutation", return_value={"giftCard": card, "giftCardCode": "PROVIDER-ABCD"}) as create:
            result = shopify_client.issue_gift_card("999", note="Refund", code="PROVIDER-ABCD")
        self.assertEqual(result["code"], "PROVIDER-ABCD")
        self.assertEqual(result["id"], "123")
        self.assertEqual(create.call_args.args[1]["input"]["initialValue"], "999")

    def test_definite_failure_allows_safe_retry_but_never_notifies_success(self):
        number = self.submit().json["return_number"]
        self.action(number, "accept-photos")
        self.action(number, "mark-parcel-received")
        with patch.object(shopify_client, "issue_gift_card", side_effect=shopify_client.ShopifyUserError("Check store currency")):
            self.assertEqual(self.action(number, "accept-parcel").status_code, 400)
        self.assertEqual(GiftCardIssuance.query.one().state, "rejected")
        self.assertIsNone(ReturnRequest.query.one().gift_card_code)
        self.assertIsNone(Notification.query.filter_by(event_key=number+":gift_card").first())
        def created(**kw):
            return {"id": 123, "currency": "INR", "initial_value": kw["amount"], "code": kw["code"], "note": kw["note"]}
        with patch.object(shopify_client, "issue_gift_card", side_effect=created) as create:
            self.assertEqual(self.action(number, "accept-parcel").status_code, 200)
        create.assert_called_once()
        self.assertEqual(GiftCardIssuance.query.one().state, "confirmed")
