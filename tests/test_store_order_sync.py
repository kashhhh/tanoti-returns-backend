import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import test_workflows as fixtures
from app.extensions import db
from app.models import Customer, Order, OrderItem, StoreOrderSync, ReturnRequest
from app.services import shopify_client
from app.services.store_order_sync import sync_store_batch


class StoreOrderSyncTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    submit = fixtures.WorkflowTests.submit

    def payload(self, number, *, customer_email=None):
        return {"id": number, "order_number": number, "updated_at": datetime.utcnow().isoformat() + "Z",
            "currency": "INR", "financial_status": "paid", "taxes_included": True, "refunds": [],
            "customer": {"id": number + 1000, "email": customer_email or f"buyer{number}@example.com",
                         "first_name": "Buyer", "last_name": str(number)},
            "created_at": "2026-09-26T12:00:00Z",
            "line_items": [{"id": number, "quantity": 1, "price": "999", "title": "Shirt", "product_id": 99,
                            "discount_allocations": [], "tax_lines": []}],
            "fulfillments": [{"id": number + 5000, "created_at": "2026-09-26T12:00:00Z", "status": "success",
                              "line_items": [{"id": number, "quantity": 1}]}]}

    def test_imports_thirty_two_store_orders_even_when_customers_never_logged_in(self):
        payloads = [self.payload(number) for number in range(201, 233)]
        delivery_dates = {str(number + 5000): (datetime.utcnow() - timedelta(days=1)).isoformat() + "Z"
                          for number in range(201, 233)}
        with patch.object(shopify_client, "fetch_store_orders_page", side_effect=[(payloads[:25], "next-page"), (payloads[25:], None)]) as pages, \
                patch.object(shopify_client, "fetch_fulfillment_delivery_dates", return_value=delivery_dates), \
                patch.object(shopify_client, "get_product_image_url") as images:
            first = self.client.post("/api/admin/eligible-orders/sync", json={}, headers=self.admin_headers)
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.json["running"])
            self.assertEqual(first.json["processed"], 25)
            self.assertIsNone(first.json["last_completed_at"])
            second = self.client.post("/api/admin/eligible-orders/sync", json={}, headers=self.admin_headers)
            self.assertFalse(second.json["running"])
            self.assertEqual(second.json["processed"], 32)
            self.assertIsNotNone(second.json["last_completed_at"])
            self.assertEqual(pages.call_args_list[1].args[0], "next-page")
            images.assert_not_called()
        self.assertEqual(Order.query.count(), 33)
        self.assertEqual(Customer.query.count(), 33)
        result = self.client.get("/api/admin/eligible-orders?per_page=50", headers=self.admin_headers).json
        self.assertEqual(result["total"], 33)
        self.assertEqual(len(result["orders"]), 33)
        self.assertFalse(result["sync"]["requires_sync"])
        self.assertEqual(next(row for row in result["orders"] if row["order_number"] == "201")["customer_name"], "Buyer 201")

    def test_failed_page_is_rolled_back_and_resumes_without_marking_complete(self):
        with patch.object(shopify_client, "fetch_store_orders_page", return_value=([self.payload(201)], "page-two")), \
                patch.object(shopify_client, "fetch_fulfillment_delivery_dates", return_value={"5201": None}):
            sync_store_batch()
        with patch.object(shopify_client, "fetch_store_orders_page", return_value=([self.payload(202)], None)), \
                patch.object(shopify_client, "fetch_fulfillment_delivery_dates", side_effect=RuntimeError("provider down")):
            response = self.client.post("/api/admin/eligible-orders/sync", json={}, headers=self.admin_headers)
            self.assertEqual(response.status_code, 503)
        state = db.session.get(StoreOrderSync, 1)
        self.assertTrue(state.running)
        self.assertEqual(state.next_page, "page-two")
        self.assertEqual(state.processed, 1)
        self.assertIsNone(state.completed_at)
        self.assertIsNotNone(state.error)
        self.assertIsNone(Order.query.filter_by(shopify_order_id="202").first())
        with patch.object(shopify_client, "fetch_store_orders_page", return_value=([self.payload(202)], None)) as pages, \
                patch.object(shopify_client, "fetch_fulfillment_delivery_dates", return_value={"5202": None}):
            result = sync_store_batch()
        self.assertEqual(pages.call_args.args[0], "page-two")
        self.assertEqual(result["processed"], 2)
        self.assertIsNone(result["error"])
        self.assertFalse(result["running"])

    def test_first_page_failure_can_be_retried_and_missing_emails_are_reported(self):
        with patch.object(shopify_client, "fetch_store_orders_page", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                sync_store_batch()
        skipped = self.payload(201)
        skipped["customer"]["email"] = None
        with patch.object(shopify_client, "fetch_store_orders_page", return_value=([skipped], None)), \
                patch.object(shopify_client, "fetch_fulfillment_delivery_dates", return_value={"5201": None}):
            status = sync_store_batch()
        self.assertEqual(status["processed"], 1)
        self.assertEqual(status["skipped"], 1)
        self.assertIsNotNone(status["last_completed_at"])

    def test_sync_endpoint_is_admin_only_and_requires_csrf(self):
        with patch.object(shopify_client, "fetch_store_orders_page") as pages:
            self.assertEqual(self.client.post("/api/admin/eligible-orders/sync", json={}).status_code, 401)
            self.assertIn(self.client.post("/api/admin/eligible-orders/sync", json={}, headers=self.customer_headers).status_code, (401, 403))
            self.assertEqual(self.client.post("/api/admin/eligible-orders/sync", json={}, headers={"Cookie": self.admin_headers["Cookie"]}).status_code, 403)
            pages.assert_not_called()

    def stale_order(self):
        item = db.session.get(OrderItem, self.item_id)
        item.order.shopify_updated_at = datetime.utcnow() - timedelta(days=1)
        item.order.delivery_checked_at = datetime.utcnow() - timedelta(hours=1)
        item.order.customer.shopify_customer_id = "1100"
        item.delivered_at = datetime.utcnow() - timedelta(days=20)
        db.session.commit()
        payload = self.payload(100, customer_email="customer@example.com")
        payload["line_items"][0]["id"] = 1
        payload["fulfillments"][0]["line_items"][0]["id"] = 1
        payload["updated_at"] = item.order.shopify_updated_at.isoformat() + "Z"
        return payload

    def test_customer_listing_refreshes_an_expired_cached_delivery(self):
        payload = self.stale_order()
        delivered = (datetime.utcnow() - timedelta(days=1)).isoformat() + "Z"
        with patch.object(shopify_client, "fetch_orders_for_customer", return_value=[payload]) as fetch, \
                patch.object(shopify_client, "fetch_fulfillment_delivery_dates", return_value={"5100": delivered}):
            result = self.client.get("/api/customer/orders", headers=self.customer_headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json["eligible"]), 1)
        self.assertEqual(result.json["eligible"][0]["delivered_at"], delivered)
        fetch.assert_called_once_with("1100")

    def test_direct_submission_rechecks_delivery_before_rejecting_old_cache(self):
        payload = self.stale_order()
        with patch.object(shopify_client, "fetch_order", return_value=payload), \
                patch.object(shopify_client, "fetch_fulfillment_delivery_dates", return_value={"5100": datetime.utcnow().isoformat() + "Z"}):
            result = self.submit()
        self.assertEqual(result.status_code, 201, result.json)

    def test_delivery_refresh_failure_is_unavailable_instead_of_false_expiry(self):
        self.stale_order()
        with patch.object(shopify_client, "fetch_orders_for_customer", side_effect=RuntimeError("unavailable")):
            result = self.client.get("/api/customer/orders", headers=self.customer_headers)
        self.assertEqual(result.status_code, 503)
        self.assertNotIn("ended", result.json["error"])
        with patch.object(shopify_client, "fetch_order", side_effect=RuntimeError("unavailable")):
            self.assertEqual(self.submit().status_code, 503)
        self.assertEqual(ReturnRequest.query.count(), 0)

    def test_missing_delivery_orders_are_reported_without_calling_them_expired(self):
        item = db.session.get(OrderItem, self.item_id)
        item.fulfilled_at = datetime.utcnow()
        item.delivered_at = None
        db.session.commit()
        result = self.client.get("/api/admin/eligible-orders", headers=self.admin_headers).json
        self.assertEqual(result["missing_delivery_orders"], 1)
        self.assertEqual(result["total"], 0)
        self.assertIsNone(result["sync"]["last_completed_at"])


class ShopifyPaginationAndEventsTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown

    def test_customer_orders_follow_all_pages_and_do_not_reapply_first_page_filters(self):
        self.app.config["SHOPIFY_STORE_DOMAIN"] = "test.myshopify.com"
        base = shopify_client._base_url() + "/customers/10/orders.json"
        next_url = base + "?page_info=second&limit=250"
        first, second = Mock(), Mock()
        first.json.return_value = {"orders": [{"id": 1}]}
        first.links = {"next": {"url": next_url}}
        second.json.return_value = {"orders": [{"id": 2}]}
        second.links = {}
        with patch.object(shopify_client, "_headers", return_value={}), patch("requests.get", side_effect=[first, second]) as get:
            self.assertEqual(shopify_client.fetch_orders_for_customer("10"), [{"id": 1}, {"id": 2}])
        self.assertEqual(get.call_args_list[1].args[0], next_url)
        self.assertIsNone(get.call_args_list[1].kwargs["params"])

    def test_order_pagination_rejects_unexpected_origins_and_paths(self):
        self.app.config["SHOPIFY_STORE_DOMAIN"] = "test.myshopify.com"
        for url in ("https://evil.example/orders.json", shopify_client._base_url() + "/customers/1/orders.json"):
            with patch("requests.get") as get:
                with self.assertRaises(ValueError):
                    shopify_client.fetch_store_orders_page(url)
                get.assert_not_called()

    def test_delivery_events_supply_actual_happened_time_on_later_page(self):
        with patch.object(shopify_client, "graphql", side_effect=[
                {"nodes": [{"id": "gid://shopify/Fulfillment/55", "deliveredAt": None, "displayStatus": "DELIVERED"}]},
                {"fulfillment": {"events": {"nodes": [{"status": "IN_TRANSIT", "happenedAt": "2026-09-27T12:00:00Z"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "events-two"}}}},
                {"fulfillment": {"events": {"nodes": [{"status": "DELIVERED", "happenedAt": "2026-09-30T12:00:00Z"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": "end"}}}}
        ]) as graphql:
            self.assertEqual(shopify_client.fetch_fulfillment_delivery_dates([55]), {"55": "2026-09-30T12:00:00Z"})
        self.assertEqual(graphql.call_args.args[1]["after"], "events-two")

    def test_delivered_status_without_a_confirmed_time_is_not_a_delivery_date(self):
        with patch.object(shopify_client, "graphql", side_effect=[
                {"nodes": [{"id": "gid://shopify/Fulfillment/55", "deliveredAt": None, "displayStatus": "DELIVERED"}]},
                {"fulfillment": {"events": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
        ]):
            self.assertEqual(shopify_client.fetch_fulfillment_delivery_dates([55]), {"55": None})
