import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import test_workflows as fixtures
from app.extensions import db
from app.models import AdminSettings, Customer, Order, OrderItem, PaymentMethod


class EligibleOrdersTests(unittest.TestCase):
    submit = fixtures.WorkflowTests.submit

    def setUp(self):
        fixtures.WorkflowTests.setUp(self)
        self.now = datetime.utcnow()
        self.clock = patch("app.services.eligible_orders.datetime")
        self.clock.start().utcnow.return_value = self.now
        db.session.get(OrderItem, self.item_id).delivered_at = self.now - timedelta(days=4)
        db.session.commit()

    def tearDown(self):
        self.clock.stop()
        fixtures.WorkflowTests.tearDown(self)

    def listing(self, query=""):
        return self.client.get("/api/admin/eligible-orders" + query, headers=self.admin_headers)

    def add_order(self, number, name="Another Customer", delivered=None):
        customer = Customer(email=f"customer-{number}@example.com", name=name)
        order = Order(shopify_order_id=str(90000 + number), order_number=str(number),
                      customer=customer, payment_method=PaymentMethod.PREPAID)
        item = OrderItem(order=order, shopify_line_item_id=str(number), product_title="Shirt",
                         price=999, delivered_at=delivered or self.now - timedelta(days=1))
        db.session.add(item)
        return item

    def test_listing_is_admin_only(self):
        self.assertEqual(self.client.get("/api/admin/eligible-orders").status_code, 401)
        self.assertIn(self.client.get("/api/admin/eligible-orders", headers=self.customer_headers).status_code, (401, 403))
        self.assertEqual(self.listing().status_code, 200)

    def test_lists_each_order_once_with_customer_and_delivery_dates(self):
        original = db.session.get(OrderItem, self.item_id)
        db.session.add(OrderItem(order=original.order, shopify_line_item_id="second", product_title="Shirt",
                                price=999, delivered_at=self.now - timedelta(days=2)))
        db.session.commit()
        data = self.listing().json
        self.assertEqual(data["total"], 1)
        row = data["orders"][0]
        self.assertEqual(row["order_number"], "100")
        self.assertEqual(row["shopify_order_id"], "100")
        self.assertEqual(row["customer_name"], "Customer")
        self.assertEqual(row["eligible_units"], 2)
        self.assertEqual(row["delivered_at"], (self.now - timedelta(days=4)).isoformat() + "Z")
        self.assertEqual(row["latest_delivered_at"], (self.now - timedelta(days=2)).isoformat() + "Z")
        self.assertEqual(row["return_deadline"], (self.now + timedelta(days=1)).isoformat() + "Z")

    def test_listing_matches_customer_eligibility_and_configured_window(self):
        for index, state in enumerate(("expired", "undelivered", "claimed", "unavailable", "superseded", "boundary"), 201):
            item = self.add_order(index)
            if state == "expired":
                item.delivered_at = self.now - timedelta(days=5, microseconds=1)
            elif state == "undelivered":
                item.delivered_at = None
                item.fulfilled_at = self.now - timedelta(days=1)
            elif state == "boundary":
                item.delivered_at = self.now - timedelta(days=5)
            else:
                setattr(item, {"claimed": "request_claimed"}.get(state, state), True)
        db.session.commit()
        with patch("app.models.datetime") as clock:
            clock.utcnow.return_value = self.now
            expected = {i.order.order_number for i in OrderItem.query.all() if i.request_block_reason(5) is None}
        self.assertEqual({r["order_number"] for r in self.listing().json["orders"]}, expected)
        AdminSettings.get().return_window_days = 3
        db.session.commit()
        self.assertEqual(self.listing().json["total"], 0)

    def test_existing_return_excludes_unit_even_without_claim_flag(self):
        self.assert_request_excludes_unit("return")

    def test_existing_exchange_excludes_unit_even_without_claim_flag(self):
        self.assert_request_excludes_unit("exchange")

    def assert_request_excludes_unit(self, kind):
        item = db.session.get(OrderItem, self.item_id)
        self.assertEqual(self.submit(kind).status_code, 201)
        item.request_claimed = False
        db.session.commit()
        self.assertEqual(self.listing().json["total"], 0)

    def test_search_matches_order_ids_names_and_email_and_escapes_wildcards(self):
        self.add_order(210, "Priya Sharma")
        self.add_order(211, "A_100%")
        db.session.commit()
        for query in ("%23210", "90210", "priya", "SHARMA", "customer-210%40example.com"):
            with self.subTest(query=query):
                data = self.listing("?q=" + query).json
                self.assertEqual([r["order_number"] for r in data["orders"]], ["210"])
        self.assertEqual(self.listing("?q=%25").json["total"], 1)
        self.assertEqual(self.listing("?q=_").json["total"], 1)
        self.assertEqual(self.listing("?q=missing").json["orders"], [])

    def test_pagination_keeps_orders_distinct_and_sorts_by_earliest_deadline(self):
        for number in range(301, 327):
            self.add_order(number)
        db.session.commit()
        first = self.listing().json
        second = self.listing("?page=2").json
        self.assertEqual(first["total"], 27)
        self.assertEqual(len(first["orders"]), 25)
        self.assertEqual(len(second["orders"]), 2)
        self.assertEqual(first["orders"][0]["order_number"], "100")
        self.assertFalse({r["id"] for r in first["orders"]} & {r["id"] for r in second["orders"]})
        self.assertEqual(self.listing("?page=999").json["page"], 2)
        for args in ("?page=0", "?page=abc", "?per_page=1000"):
            self.assertEqual(self.listing(args).status_code, 400)
