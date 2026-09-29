import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import test_workflows as fixtures
from app.extensions import db
from app.models import ReturnRequest, ShippingBooking, RequestStatus, Notification, GiftCardIssuance
from app.services import refund_service, shipping_service


def order(price="1000.00", discount="200.00", tax="40.00", quantity=1, included=True):
    return {"id": "100", "currency": "INR", "financial_status": "paid", "taxes_included": included,
        "refunds": [], "line_items": [{"id": "1", "price": price, "quantity": quantity,
            "discount_allocations": [{"amount": discount}], "tax_lines": [{"price": tax}]}]}


class RefundValueTests(unittest.TestCase):
    def test_tax_inclusive_and_exclusive_discounts(self):
        quote = refund_service.unit_value(order(), "1", 1, "INR")
        self.assertEqual(Decimal(quote["paid_amount"]), Decimal("800"))
        quote = refund_service.unit_value(order(included=False), "1", 1, "INR")
        self.assertEqual(Decimal(quote["paid_amount"]), Decimal("840"))
        self.assertEqual(quote["shipping_refund"], "0.00")

    def test_odd_paise_allocation_preserves_line_total(self):
        values = [refund_service.unit_value(order(price="100", discount="1", tax="0.11", quantity=3, included=False), "1", unit, "INR") for unit in (1, 2, 3)]
        self.assertEqual(sum(Decimal(q["paid_amount"]) for q in values), Decimal("299.11"))
        self.assertEqual(sum(Decimal(q["discount"]) for q in values), Decimal("1"))
        self.assertEqual(sum(Decimal(q["tax"]) for q in values), Decimal("0.11"))

    def test_missing_currency_unpaid_and_external_refunds_block(self):
        for changes in ({"currency": "USD"}, {"financial_status": "pending"}, {"taxes_included": None},
                        {"cancelled_at": "2026-09-01"}, {"refunds": [{"refund_line_items": [{"line_item_id": "1", "quantity": 1}]}]},
                        {"refunds": [{"refund_line_items": []}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                refund_service.unit_value({**order(), **changes}, "1", 1, "INR")
        for bad in ("NaN", "Infinity", "-1", "0.001", None):
            with self.assertRaises(ValueError):
                refund_service.money(bad)


class ReturnOperationsTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    submit = fixtures.WorkflowTests.submit
    action = fixtures.WorkflowTests.action

    def inspected(self):
        number = self.submit().json["return_number"]
        req = ReturnRequest.query.one()
        req.status = RequestStatus.PARCEL_RECEIVED
        req.parcel_received_at = datetime.utcnow()
        db.session.commit()
        return number, req

    def test_discounted_amount_is_snapshotted_and_used_for_card(self):
        with patch("app.services.shopify_client.fetch_order", return_value=order()):
            number, req = self.inspected()
            self.assertEqual(req.net_refund_amount, Decimal("800"))
            with patch("app.services.shopify_client.issue_gift_card", side_effect=lambda **kw: {
                "id": "777", "code": kw["code"], "initial_value": kw["amount"], "currency": "INR", "note": kw["note"]}) as issue:
                response = self.action(number, "accept-parcel")
                self.assertEqual(response.status_code, 200, response.json)
                self.assertEqual(Decimal(issue.call_args.kwargs["amount"]), Decimal("800"))

    def test_changed_price_blocks_card_until_staff_reviews(self):
        number, req = self.inspected()
        with patch("app.services.shopify_client.fetch_order", return_value=order()), patch("app.services.shopify_client.issue_gift_card") as issue:
            response = self.action(number, "accept-parcel")
            self.assertEqual(response.status_code, 409)
            self.assertTrue(response.json["refund_review_required"])
            issue.assert_not_called()
            self.assertEqual(req.net_refund_amount, Decimal("999"))
            preview = self.client.get(f"/api/admin/requests/return/{number}/review-refund", headers=self.admin_headers)
            self.assertEqual(Decimal(preview.json["net_refund_amount"]), Decimal("800"))
            self.assertEqual(self.action(number, "review-refund", expected_amount="799").status_code, 409)
            self.assertEqual(self.action(number, "review-refund", expected_amount="800").status_code, 200)
            self.assertEqual(req.net_refund_amount, Decimal("800"))

    def test_provider_failure_and_external_refund_block_approval(self):
        number, req = self.inspected()
        for result in (RuntimeError("Unavailable"), {**order(), "refunds": [{"refund_line_items": []}]}):
            args = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with patch("app.services.shopify_client.fetch_order", **args), patch("app.services.shopify_client.issue_gift_card") as issue:
                self.assertEqual(self.action(number, "accept-parcel").status_code, 409)
                issue.assert_not_called()
        self.assertEqual(req.status, RequestStatus.PARCEL_RECEIVED)

    def test_zero_refund_completes_without_creating_a_card_or_claiming_payment(self):
        with patch("app.services.shopify_client.fetch_order", return_value=order(discount="1000", tax="0")):
            number, req = self.inspected()
            with patch("app.services.shopify_client.issue_gift_card") as issue:
                result = self.action(number, "accept-parcel")
                self.assertEqual(result.status_code, 200, result.json)
                self.assertEqual(result.json["stage"], "no_refund_due")
                issue.assert_not_called()
            self.assertIsNone(req.refund_paid_at)
            self.assertIsNone(req.gift_card_code)
            self.assertIsNone(req.order_item.active_request())
            self.assertIsNone(Notification.query.filter_by(event_key=number + ":gift_card").first())
            history = self.client.get("/api/customer/my-requests", headers=self.customer_headers).json["requests"][0]
            self.assertTrue(history["is_past"])
            self.assertIn("no refund due", history["timeline"][-1]["label"])

    def test_customer_stale_quote_does_not_create_a_request(self):
        response = self.submit(expected_refund_amount="100")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ReturnRequest.query.count(), 0)

    def test_deduction_applies_after_discounts_and_tax(self):
        from app.models import AdminSettings
        settings = AdminSettings.get()
        settings.deduction_enabled, settings.deduction_amount = True, Decimal("100")
        db.session.commit()
        with patch("app.services.shopify_client.fetch_order", return_value=order(included=False)):
            result = self.submit()
            self.assertEqual(result.status_code, 201, result.json)
            self.assertEqual(Decimal(result.json["net_refund_amount"]), Decimal("740"))

    def test_legacy_uncertain_gift_card_cannot_be_revalued(self):
        number, req = self.inspected()
        req.parcel_decision_at = datetime.utcnow()
        db.session.commit()
        self.assertEqual(self.action(number, "review-refund", expected_amount="800").status_code, 409)

    def test_uncertain_credit_cannot_be_revalued(self):
        number, req = self.inspected()
        db.session.add(GiftCardIssuance(return_id=req.id, code="ABC123", amount=999, state="uncertain", requested_by="staff@example.com"))
        db.session.commit()
        self.assertEqual(self.action(number, "review-refund", expected_amount="800").status_code, 409)

    def test_attention_threshold_and_email_recovery(self):
        number = self.submit().json["return_number"]
        req = ReturnRequest.query.one()
        self.assertEqual(self.client.get("/api/admin/requests?stage=attention", headers=self.admin_headers).json["total"], 0)
        req.created_at = datetime.utcnow() - timedelta(hours=25)
        db.session.commit()
        data = self.client.get("/api/admin/requests?stage=attention", headers=self.admin_headers).json
        self.assertEqual(data["total"], 1)
        self.assertIn("Photo review overdue", data["requests"][0]["attention_reasons"])
        self.assertEqual(data["counts"]["all"], 1)
        req.created_at = datetime.utcnow()
        note = Notification.query.one()
        note.sent_at, note.attempts = None, 1
        db.session.commit()
        self.assertEqual(self.client.get("/api/admin/requests?stage=attention", headers=self.admin_headers).json["total"], 1)
        self.assertEqual(self.action(number, "retry-emails").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/requests?stage=attention", headers=self.admin_headers).json["total"], 0)

    def booking(self):
        number = self.submit().json["return_number"]
        req = ReturnRequest.query.one()
        req.status, req.pickup_carrier, req.pickup_tracking_id = RequestStatus.PICKUP_SCHEDULED, "delhivery", "1234567890123"
        req.photo_decision_at = datetime.utcnow()
        self.app.config.update(DELHIVERY_API_TOKEN="test-token", DELHIVERY_ENVIRONMENT="staging", DELHIVERY_PICKUP_LOCATION="Warehouse")
        db.session.add(ShippingBooking(reference=number + "-R", request_number=number, leg="pickup", environment="staging",
            warehouse="Warehouse", state="confirmed", waybill=req.pickup_tracking_id))
        db.session.commit()
        return number, req

    def tracking(self, number, status="Scheduled", when=None):
        return {"AWB": "1234567890123", "ReferenceNo": number + "-R", "Status": {"Status": status,
                "StatusDateTime": (when or (datetime.utcnow() + timedelta(seconds=1))).isoformat() + "Z"}}

    def test_cancellation_requires_carrier_confirmation_and_keeps_case_open(self):
        number, req = self.booking()
        response = Mock(status_code=200)
        with patch.object(shipping_service, "fetch_tracking", side_effect=[self.tracking(number), self.tracking(number, "Canceled")]), patch.object(shipping_service.requests, "post", return_value=response) as post:
            result = self.action(number, "cancel-shipment", leg="pickup")
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(result.json["shipping_bookings"]["pickup"]["action_state"], "confirmed")
            self.assertEqual(result.json["stage"], "pickup_cancelled")
            self.assertEqual(req.status, RequestStatus.PICKUP_SCHEDULED)
            history = self.client.get("/api/customer/my-requests", headers=self.customer_headers).json["requests"][0]
            self.assertFalse(history["is_past"])
            self.assertEqual(history["timeline"][-1]["label"], "Pickup cancelled")
            self.assertEqual(post.call_args.kwargs["json"], {"waybill": "1234567890123", "cancellation": "true"})
        with patch.object(shipping_service, "fetch_tracking", return_value=self.tracking(number, "Canceled")), patch.object(shipping_service.requests, "post") as post:
            self.assertEqual(self.action(number, "cancel-shipment", leg="pickup").status_code, 200)
            post.assert_not_called()

    def test_cancel_timeout_fences_retries_and_appears_in_attention(self):
        number, req = self.booking()
        with patch.object(shipping_service, "fetch_tracking", return_value=self.tracking(number)), patch.object(shipping_service.requests, "post", side_effect=TimeoutError()) as post:
            self.assertEqual(self.action(number, "cancel-shipment", leg="pickup").status_code, 409)
            self.assertEqual(self.action(number, "cancel-shipment", leg="pickup").status_code, 409)
            self.assertEqual(post.call_count, 1)
        data = self.client.get("/api/admin/requests?stage=attention", headers=self.admin_headers).json
        self.assertEqual(data["total"], 1)
        self.assertIn("Shipment action needs confirmation", data["requests"][0]["attention_reasons"])

    def test_http_success_without_new_carrier_scan_remains_unconfirmed(self):
        number, req = self.booking()
        old_cancel = self.tracking(number, "Canceled", datetime.utcnow() - timedelta(hours=1))
        with patch.object(shipping_service, "fetch_tracking", side_effect=[self.tracking(number), old_cancel]), patch.object(shipping_service.requests, "post", return_value=Mock(status_code=200)):
            result = self.action(number, "cancel-shipment", leg="pickup")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json["shipping_bookings"]["pickup"]["action_state"], "submitted")

    def test_forward_cancellation_uses_returned_status(self):
        from app.models import ExchangeRequest
        number = self.submit("exchange").json["exchange_number"]
        req = ExchangeRequest.query.one()
        req.status, req.outbound_carrier, req.outbound_tracking_id = RequestStatus.COMPLETED, "delhivery", "1234567890123"
        self.app.config.update(DELHIVERY_API_TOKEN="test-token", DELHIVERY_ENVIRONMENT="staging")
        db.session.add(ShippingBooking(reference=number + "-F", request_number=number, leg="replacement", environment="staging",
            warehouse="Warehouse", state="confirmed", waybill=req.outbound_tracking_id))
        db.session.commit()
        before = {**self.tracking(number), "ReferenceNo": number + "-F"}
        after = {**self.tracking(number, "Returned"), "ReferenceNo": number + "-F"}
        with patch.object(shipping_service, "fetch_tracking", side_effect=[before, after]), patch.object(shipping_service.requests, "post", return_value=Mock(status_code=200)):
            result = self.action(number, "cancel-shipment", kind="exchange", leg="replacement")
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["stage"], "replacement_cancelled")
        self.assertEqual(result.json["shipping_bookings"]["replacement"]["action_state"], "confirmed")
        self.assertIsNone(req.delivered_at)
        self.assertEqual(self.action(number, "mark-delivered", kind="exchange").status_code, 409)

    def test_attention_thresholds_cover_inspection_and_delivery_and_respect_filters(self):
        from app.models import ExchangeRequest
        number = self.submit("exchange").json["exchange_number"]
        req = ExchangeRequest.query.one()
        req.status = RequestStatus.PARCEL_RECEIVED
        req.parcel_received_at = datetime.utcnow() - timedelta(days=3)
        db.session.commit()
        data = self.client.get("/api/admin/requests?stage=attention&type=exchange", headers=self.admin_headers).json
        self.assertIn("Inspection overdue", data["requests"][0]["attention_reasons"])
        self.assertEqual(self.client.get("/api/admin/requests?stage=attention&type=return", headers=self.admin_headers).json["total"], 0)
        req.status, req.outbound_tracking_id = RequestStatus.COMPLETED, "1234567890123"
        req.parcel_decision_at = datetime.utcnow() - timedelta(days=8)
        db.session.commit()
        data = self.client.get("/api/admin/requests?stage=attention", headers=self.admin_headers).json
        self.assertIn("Replacement delivery overdue", data["requests"][0]["attention_reasons"])
        req.delivered_at = datetime.utcnow()
        db.session.commit()
        self.assertEqual(self.client.get("/api/admin/requests?stage=attention", headers=self.admin_headers).json["total"], 0)

    def test_cannot_cancel_moving_or_mismatched_shipments_or_as_customer(self):
        number, req = self.booking()
        for shipment in (self.tracking(number, "In Transit"), self.tracking("RET-WRONG")):
            with patch.object(shipping_service, "fetch_tracking", return_value=shipment), patch.object(shipping_service.requests, "post") as post:
                self.assertEqual(self.action(number, "cancel-shipment", leg="pickup").status_code, 409)
                post.assert_not_called()
        response = self.client.post(f"/api/admin/requests/return/{number}/cancel-shipment", headers=self.customer_headers, json={"leg": "pickup"})
        self.assertEqual(response.status_code, 401)
