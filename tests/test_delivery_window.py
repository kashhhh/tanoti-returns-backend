import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import test_workflows as fixtures
from app.extensions import db
from app.models import AdminSettings, OrderItem
from app.services import shopify_client
from app.services.sync_service import upsert_order


class DeliveryWindowTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    submit = fixtures.WorkflowTests.submit

    def payload(self, count=1):
        return {"id": 100, "order_number": 100, "updated_at": "2026-10-06T12:00:00Z",
                "customer": {"id": 10, "email": "customer@example.com"},
                "line_items": [{"id": 1, "title": "Shirt", "price": "999", "quantity": count}],
                "fulfillments": [{"id": 55, "status": "success", "created_at": "2026-10-01T10:00:00+05:30",
                                  "line_items": [{"id": 1, "quantity": 1}]}]}

    def test_october_six_delivery_is_eligible_on_october_ten(self):
        self.assert_october_delivery_submission("return")

    def test_october_six_delivery_can_be_exchanged_on_october_ten(self):
        self.assert_october_delivery_submission("exchange")

    def assert_october_delivery_submission(self, kind):
        item = db.session.get(OrderItem, self.item_id)
        item.order.fulfilled_at = datetime(2026, 10, 1)
        item.delivered_at = datetime(2026, 10, 6, 4, 30)
        db.session.commit()
        with patch("app.models.datetime") as clock:
            clock.utcnow.return_value = datetime(2026, 10, 10, 12)
            row = self.client.get("/api/customer/orders", headers=self.customer_headers).json["eligible"][0]
            self.assertEqual(row["return_deadline"], "2026-10-11T04:30:00Z")
            self.assertEqual(self.submit(kind).status_code, 201)

    def test_exact_five_day_boundary_and_expiry(self):
        item = db.session.get(OrderItem, self.item_id)
        item.delivered_at = datetime(2026, 10, 6, 4, 30)
        deadline = item.request_deadline(5)
        with patch("app.models.datetime") as clock:
            clock.utcnow.return_value = deadline
            self.assertIsNone(item.request_block_reason(5))
            clock.utcnow.return_value = deadline + timedelta(microseconds=1)
            self.assertIn("ended", item.request_block_reason(5))
            for kind in ("return", "exchange"):
                self.assertEqual(self.submit(kind).status_code, 400)

    def test_september_twenty_six_order_uses_september_thirty_delivery(self):
        item = db.session.get(OrderItem, self.item_id)
        item.order.created_at = datetime(2026, 9, 26, 12)
        item.order.fulfilled_at = datetime(2026, 9, 26, 12)
        item.delivered_at = datetime(2026, 9, 30, 12)
        self.assertEqual(item.request_deadline(5), datetime(2026, 10, 5, 12))
        with patch("app.models.datetime") as clock:
            clock.utcnow.return_value = datetime(2026, 10, 1, 12)
            self.assertIsNone(item.request_block_reason(5))
            clock.utcnow.return_value = datetime(2026, 10, 6, 12)
            self.assertIn("ended", item.request_block_reason(5))

    def test_fulfillment_without_delivery_does_not_start_clock(self):
        item = db.session.get(OrderItem, self.item_id)
        item.delivered_at = None
        db.session.commit()
        row = self.client.get("/api/customer/orders", headers=self.customer_headers).json["expired"][0]
        self.assertTrue(row["fulfilled"])
        self.assertFalse(row["delivered"])
        self.assertIsNone(row["return_deadline"])
        self.assertIn("delivery", row["unavailable_reason"])
        for kind in ("return", "exchange"):
            self.assertEqual(self.submit(kind).status_code, 400)

    def test_split_shipments_have_independent_delivery_clocks(self):
        payload = self.payload(3)
        payload["fulfillments"].append({"id": 56, "status": "success", "created_at": "2026-10-02T10:00:00+05:30",
                                        "line_items": [{"id": 1, "quantity": 1}]})
        with patch.object(shopify_client, "fetch_fulfillment_delivery_dates", return_value={
                "55": "2026-10-06T10:00:00+05:30", "56": None}):
            upsert_order(payload)
        units = OrderItem.query.order_by(OrderItem.unit_number).all()
        self.assertEqual(units[0].request_deadline(5), datetime(2026, 10, 11, 4, 30))
        self.assertIsNone(units[1].request_deadline(5))
        self.assertIsNone(units[2].request_deadline(5))
        self.assertEqual(AdminSettings.get().return_window_days, 5)

    def test_equal_order_timestamp_refreshes_delivery_without_replaying_fields(self):
        payload = self.payload()
        with patch.object(shopify_client, "fetch_fulfillment_delivery_dates", return_value={"55": None}):
            order = upsert_order(payload)
        self.assertIsNone(order.items[0].delivered_at)
        order.financial_status = "refunded"
        db.session.commit()
        payload["financial_status"] = "paid"
        with patch.object(shopify_client, "fetch_fulfillment_delivery_dates", return_value={"55": "2026-10-06T12:00:00Z"}):
            upsert_order(payload)
        self.assertEqual(order.financial_status, "refunded")
        self.assertEqual(order.items[0].delivered_at, datetime(2026, 10, 6, 12))
        payload["updated_at"] = "2026-10-05T12:00:00Z"
        with patch.object(shopify_client, "fetch_fulfillment_delivery_dates") as fetch:
            upsert_order(payload)
            fetch.assert_not_called()

    def test_graphql_reads_actual_delivery_and_preserves_missing_confirmation(self):
        with patch.object(shopify_client, "graphql", return_value={"nodes": [
                {"id": "gid://shopify/Fulfillment/55", "deliveredAt": "2026-10-06T12:00:00Z"},
                {"id": "gid://shopify/Fulfillment/56", "deliveredAt": None}]}) as query:
            dates = shopify_client.fetch_fulfillment_delivery_dates([55, 56, 55])
        self.assertEqual(dates, {"55": "2026-10-06T12:00:00Z", "56": None})
        self.assertEqual(query.call_args.args[1]["ids"], ["gid://shopify/Fulfillment/55", "gid://shopify/Fulfillment/56"])
        with patch.object(shopify_client, "graphql", return_value={"nodes": [None]}):
            with self.assertRaises(ValueError):
                shopify_client.fetch_fulfillment_delivery_dates([55])
