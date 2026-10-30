import unittest
from datetime import datetime
from unittest.mock import patch
import test_workflows as fixtures
from app.extensions import db
from app.models import ReturnRequest, ExchangeRequest, ShippingBooking, Order
from app.services import shipping_service as shipping

class ManualProcessingTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    submit = fixtures.WorkflowTests.submit
    action = fixtures.WorkflowTests.action

    def test_manual_approval_never_books_with_carrier(self):
        number = self.submit("exchange").json["exchange_number"]
        with patch.object(shipping, "schedule_reverse_pickup") as reverse, patch.object(shipping, "schedule_forward_shipment") as forward:
            self.assertEqual(self.action(number, "accept-photos", kind="exchange", manual_shipping=True).status_code, 200)
            self.action(number, "mark-parcel-received", kind="exchange")
            self.assertEqual(self.action(number, "accept-parcel", kind="exchange", manual_shipping=True).status_code, 200)
            reverse.assert_not_called()
            forward.assert_not_called()
        self.assertEqual(ShippingBooking.query.count(), 0)

    def test_manual_pickup_without_tracking_and_stage_filter(self):
        number = self.submit().json["return_number"]
        self.action(number, "accept-photos")
        result = self.action(number, "process-manually", leg="pickup", confirmed=True)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["stage"], "awaiting_parcel")
        self.assertIsNone(result.json["pickup_tracking_id"])
        self.assertNotIn("Pickup booking missing", result.json["attention_reasons"])
        page = self.client.get("/api/admin/requests?stage=awaiting_parcel", headers=self.admin_headers)
        self.assertEqual(page.json["total"], 1)
        self.assertEqual(self.action(number, "mark-parcel-received").status_code, 200)

    def test_uncertain_booking_cannot_block_manual_exchange_delivery(self):
        number = self.submit("exchange").json["exchange_number"]
        self.action(number, "accept-photos", kind="exchange")
        self.action(number, "mark-parcel-received", kind="exchange")
        self.action(number, "accept-parcel", kind="exchange")
        booking = shipping.current_booking(number, "replacement")
        booking.state = "uncertain"
        db.session.commit()
        with patch.object(shipping, "check_booking", side_effect=shipping.BookingUncertain("Unavailable")):
            result = self.action(number, "process-manually", kind="exchange", leg="replacement", confirmed=True)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["stage"], "shipment_booked")
        self.assertTrue(result.json["shipping_bookings"]["replacement"]["action_error"])
        self.assertEqual(self.action(number, "book-shipment", kind="exchange", leg="replacement").status_code, 409)
        with patch.object(shipping, "fetch_tracking") as tracking:
            shipping.refresh_tracking(ExchangeRequest.query.one(), "exchange")
            tracking.assert_not_called()
        self.assertEqual(self.action(number, "mark-delivered", kind="exchange").status_code, 200)

    def test_existing_booking_cancelled_and_repeated_submit_is_idempotent(self):
        number = self.submit().json["return_number"]
        self.action(number, "accept-photos")
        booking = shipping.current_booking(number, "pickup")
        booking.state = "confirmed"
        booking.waybill = "1234567890123"
        db.session.commit()
        def cancel(req, kind, leg):
            booking.carrier_status = "Cancelled"
        with patch.object(shipping, "check_booking", return_value=("delhivery", booking.waybill, "Manifested")), patch.object(shipping, "cancel_shipment", side_effect=cancel) as cancelled:
            for _ in range(2):
                result = self.action(number, "process-manually", leg="pickup", confirmed=True)
                self.assertEqual(result.status_code, 200, result.json)
            cancelled.assert_called_once()
        self.assertIsNone(result.json["shipping_bookings"]["pickup"]["action_error"])

    def test_manual_action_rejects_wrong_stage_and_invalid_input(self):
        number = self.submit().json["return_number"]
        self.assertEqual(self.action(number, "process-manually", leg="pickup", confirmed=True).status_code, 409)
        self.action(number, "accept-photos")
        for data in ({"confirmed": False}, {"confirmed": True, "tracking_id": "bad / tracking"}):
            self.assertEqual(self.action(number, "process-manually", leg="pickup", **data).status_code, 400)
        self.assertEqual(self.action(number, "process-manually", leg="replacement", confirmed=True).status_code, 400)

    def test_original_order_date_is_fetched_and_cached_not_import_date(self):
        number = self.submit().json["return_number"]
        url = f"/api/admin/requests/return/{number}/order-date"
        with patch("app.services.shopify_client.fetch_order", return_value={"id": 100, "created_at": "2025-01-02T10:30:00+05:30"}) as fetch:
            for _ in range(2):
                result = self.client.get(url, headers=self.admin_headers)
                self.assertEqual(result.json["order_date"], "2025-01-02T05:00:00Z")
            fetch.assert_called_once()
        self.assertNotEqual(Order.query.one().created_at, Order.query.one().shopify_created_at)

if __name__ == "__main__":
    unittest.main()
