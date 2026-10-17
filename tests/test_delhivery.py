import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import test_workflows as fixtures
from app.extensions import db
from app.models import OrderItem, ReturnRequest, ExchangeRequest, ShippingBooking, Notification
from app.services import shipping_service as shipping


class DelhiveryTests(unittest.TestCase):
    tearDown = fixtures.WorkflowTests.tearDown
    submit = fixtures.WorkflowTests.submit
    action = fixtures.WorkflowTests.action

    def setUp(self):
        fixtures.WorkflowTests.setUp(self)
        self.app.config.update(DELHIVERY_API_TOKEN="test-token", DELHIVERY_ENVIRONMENT="staging",
                               DELHIVERY_PICKUP_LOCATION="Tanoti Warehouse")

    def response(self, number, leg, waybill="1234567890123"):
        response = Mock(status_code=200)
        response.json.return_value = {"success": True, "packages": [{"status": "Success", "waybill": waybill,
            "refnum": shipping.reference_for(number, leg)}]}
        return response

    def test_return_collection_uses_customer_address_warehouse_and_fixed_parcel(self):
        number = self.submit().json["return_number"]
        with patch.object(shipping.requests, "post", return_value=self.response(number, "pickup")) as post:
            result = self.action(number, "accept-photos")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["pickup_tracking_id"], "1234567890123")
        payload = json.loads(post.call_args.kwargs["data"]["data"])
        self.assertEqual(post.call_args.kwargs["data"]["format"], "json")
        self.assertEqual(payload["pickup_location"], {"name": "Tanoti Warehouse"})
        parcel = payload["shipments"][0]
        self.assertEqual(parcel["payment_mode"], "Pickup")
        self.assertEqual(parcel["add"], "20 Pickup Road")
        self.assertEqual({key: parcel[key] for key in shipping.PARCEL}, shipping.PARCEL)
        self.assertNotIn("return_add", parcel)
        self.assertEqual(ShippingBooking.query.one().state, "confirmed")
        self.assertEqual(Notification.query.filter_by(event_key=number+":pickup_booked").count(), 1)

    def test_exchange_legs_have_distinct_references_and_no_extra_warehouse_pickup(self):
        number = self.submit("exchange").json["exchange_number"]
        with patch.object(shipping.requests, "post", side_effect=[self.response(number, "pickup"), self.response(number, "replacement", "2234567890123")]) as post:
            self.action(number, "accept-photos", kind="exchange")
            self.action(number, "mark-parcel-received", kind="exchange")
            result = self.action(number, "accept-parcel", kind="exchange")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(post.call_count, 2)
        reverse, forward = [json.loads(c.kwargs["data"]["data"])["shipments"][0] for c in post.call_args_list]
        self.assertNotEqual(reverse["order"], forward["order"])
        self.assertEqual(forward["payment_mode"], "Prepaid")
        self.assertEqual(forward["cod_amount"], "0")
        self.assertEqual(forward["add"], "10 Original Road")
        self.assertIn("size M", forward["products_desc"])
        self.assertNotIn("sku", forward)
        self.assertEqual(ShippingBooking.query.count(), 2)

    def test_timeout_is_fenced_and_manual_override_is_blocked(self):
        number = self.submit().json["return_number"]
        with patch.object(shipping.requests, "post", side_effect=TimeoutError()) as post:
            self.assertEqual(self.action(number, "accept-photos").status_code, 200)
            self.assertEqual(self.action(number, "book-shipment", leg="pickup").status_code, 409)
            post.assert_called_once()
        self.assertEqual(ShippingBooking.query.one().state, "uncertain")
        self.assertEqual(self.action(number, "confirm-shipment", leg="pickup", carrier="delhivery", tracking_id="1234567890123").status_code, 409)
        self.assertIsNone(Notification.query.filter_by(event_key=number+":pickup_booked").first())

    def test_missing_config_can_be_retried_after_fix(self):
        self.app.config["DELHIVERY_API_TOKEN"] = None
        number = self.submit().json["return_number"]
        with patch.object(shipping.requests, "post") as post:
            self.action(number, "accept-photos")
            post.assert_not_called()
        self.assertEqual(ShippingBooking.query.one().state, "not_attempted")
        self.app.config["DELHIVERY_API_TOKEN"] = "test-token"
        with patch.object(shipping.requests, "post", return_value=self.response(number, "pickup")) as post:
            self.assertEqual(self.action(number, "book-shipment", leg="pickup").status_code, 200)
            self.assertEqual(self.action(number, "book-shipment", leg="pickup").status_code, 200)
            post.assert_called_once()

    def test_partial_provider_failure_is_not_safe_to_rebook(self):
        number = self.submit().json["return_number"]
        response = Mock(status_code=200)
        response.json.return_value = {"success": False, "packages": []}
        with patch.object(shipping.requests, "post", return_value=response):
            self.action(number, "accept-photos")
        self.assertEqual(ShippingBooking.query.one().state, "uncertain")
        self.assertIsNone(ReturnRequest.query.one().pickup_tracking_id)

    def test_auth_rejection_allows_retry_without_ambiguous_booking(self):
        number = self.submit().json["return_number"]
        with patch.object(shipping.requests, "post", return_value=Mock(status_code=401)):
            self.action(number, "accept-photos")
        self.assertEqual(ShippingBooking.query.one().state, "not_attempted")

    def test_durable_confirmed_waybill_is_reused_after_local_finalization_crash(self):
        number = self.submit().json["return_number"]
        with patch.object(shipping.requests, "post", return_value=self.response(number, "pickup")) as post, \
                patch("app.admin.routes.queue_update", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.action(number, "accept-photos")
        db.session.rollback()
        with patch.object(shipping.requests, "post") as post:
            self.assertEqual(self.action(number, "book-shipment", leg="pickup").status_code, 200)
            post.assert_not_called()
        self.assertEqual(ReturnRequest.query.one().pickup_tracking_id, "1234567890123")

    def test_reconciliation_requires_matching_reference_and_notifies_once(self):
        number = self.submit().json["return_number"]
        with patch.object(shipping.requests, "post", side_effect=TimeoutError()):
            self.action(number, "accept-photos")
        with patch.object(shipping, "fetch_tracking", return_value={"ReferenceNo": "wrong", "AWB": "1234567890123"}):
            self.assertEqual(self.action(number, "reconcile-shipment", leg="pickup", waybill="1234567890123").status_code, 400)
        with patch.object(shipping, "fetch_tracking", return_value={"ReferenceNo": number+"-R", "AWB": "1234567890123", "Status": {"Status": "Scheduled"}}), patch.object(shipping.requests, "post") as post:
            self.assertEqual(self.action(number, "reconcile-shipment", leg="pickup", waybill="1234567890123").status_code, 200)
            self.assertEqual(self.action(number, "reconcile-shipment", leg="pickup", waybill="1234567890123").status_code, 200)
            post.assert_not_called()
        self.assertEqual(Notification.query.filter_by(event_key=number+":pickup_booked").count(), 1)

    def test_tracking_rejects_unrelated_waybill(self):
        response = Mock()
        response.json.return_value = {"ShipmentData": [{"Shipment": {"AWB": "9999999999999"}}]}
        with patch.object(shipping.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                shipping.fetch_tracking("1234567890123")

    def test_carrier_delivery_updates_exchange_and_emails_once(self):
        number = self.submit("exchange").json["exchange_number"]
        with patch.object(shipping.requests, "post", side_effect=[self.response(number, "pickup"), self.response(number, "replacement", "2234567890123")]):
            self.action(number, "accept-photos", kind="exchange")
            self.action(number, "mark-parcel-received", kind="exchange")
            self.action(number, "accept-parcel", kind="exchange")
        req = ExchangeRequest.query.one()
        req.created_at -= timedelta(days=1)
        db.session.commit()
        def tracking(waybill):
            reverse = waybill == "1234567890123"
            return {"AWB": waybill, "ReferenceNo": number + ("-R" if reverse else "-F"),
                "Status": {"Status": "Delivered", "StatusType": "DL", "StatusDateTime": datetime.utcnow().isoformat()+"Z"}}
        with patch.object(shipping, "fetch_tracking", side_effect=tracking):
            self.assertEqual(self.action(number, "refresh-tracking", kind="exchange").status_code, 200)
            self.assertEqual(self.action(number, "refresh-tracking", kind="exchange").status_code, 200)
        self.assertIsNotNone(req.delivered_at)
        self.assertEqual(OrderItem.query.filter_by(replacement_for=number).count(), 1)
        self.assertEqual(Notification.query.filter_by(event_key=number+":delivered").count(), 1)

    def test_invalid_address_never_contacts_carrier(self):
        number = self.submit().json["return_number"]
        req = ReturnRequest.query.one()
        req.pickup_address = {**req.pickup_address, "zip": "bad"}
        db.session.commit()
        with patch.object(shipping.requests, "post") as post:
            self.action(number, "accept-photos")
            post.assert_not_called()
        self.assertEqual(ShippingBooking.query.one().state, "not_attempted")

    def test_shipping_endpoints_require_admin(self):
        number = self.submit().json["return_number"]
        for endpoint in ("book-shipment", "reconcile-shipment", "refresh-tracking"):
            result = self.client.post(f"/api/admin/requests/return/{number}/{endpoint}", json={"leg": "pickup"}, headers=self.customer_headers)
            self.assertEqual(result.status_code, 401)

    def test_polling_job_reads_tracking_without_creating_shipments(self):
        number = self.submit().json["return_number"]
        with patch.object(shipping.requests, "post", return_value=self.response(number, "pickup")):
            self.action(number, "accept-photos")
        with patch.object(shipping, "fetch_tracking", return_value={"ReferenceNo": number+"-R",
                "Status": {"Status": "In Transit", "StatusType": "RT"}}) as fetch, \
                patch.object(shipping.requests, "post") as create:
            result = self.app.test_cli_runner().invoke(args=["sync-delhivery"])
            self.assertEqual(result.exit_code, 0, str(result.exception))
            fetch.assert_called_once_with("1234567890123")
            create.assert_not_called()
        self.assertEqual(ReturnRequest.query.one().pickup_status, "In Transit")
        self.assertIsNone(ReturnRequest.query.one().parcel_received_at)

    def test_wrong_environment_cannot_reuse_or_reconcile_a_booking(self):
        number = self.submit().json["return_number"]
        with patch.object(shipping.requests, "post", side_effect=TimeoutError()):
            self.action(number, "accept-photos")
        self.app.config["DELHIVERY_ENVIRONMENT"] = "production"
        with patch.object(shipping, "fetch_tracking") as fetch:
            self.assertEqual(self.action(number, "reconcile-shipment", leg="pickup", waybill="1234567890123").status_code, 400)
            fetch.assert_not_called()

    def test_request_is_not_reported_booked_for_mismatched_provider_reference(self):
        number = self.submit().json["return_number"]
        with patch.object(shipping.requests, "post", return_value=self.response("RET-OTHER", "pickup")):
            self.action(number, "accept-photos")
        self.assertEqual(ShippingBooking.query.one().state, "uncertain")
        self.assertIsNone(ReturnRequest.query.one().pickup_tracking_id)
        self.assertIsNone(Notification.query.filter_by(event_key=number+":pickup_booked").first())

    def test_worker_crash_after_fence_prevents_repeated_creation(self):
        number = self.submit().json["return_number"]
        with patch.object(shipping.requests, "post", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.action(number, "accept-photos")
        db.session.rollback()
        self.assertEqual(ShippingBooking.query.one().state, "pending")
        with patch.object(shipping.requests, "post") as create:
            self.assertEqual(self.action(number, "book-shipment", leg="pickup").status_code, 409)
            create.assert_not_called()

    def booked_pickup(self):
        number = self.submit().json["return_number"]
        with patch.object(shipping.requests, "post", return_value=self.response(number, "pickup")):
            self.action(number, "accept-photos")
        return number

    def tracking(self, reference, status="Canceled", **changes):
        return {"ReferenceNo": reference, "AWB": "1234567890123", "Status": {
            "Status": status, "StatusDateTime": datetime.utcnow().isoformat() + "Z", **changes}}

    def rebook_response(self, number, leg="pickup", attempt=2):
        response = self.response(number, leg, "2234567890123")
        response.json.return_value["packages"][0]["refnum"] = shipping.reference_for(number, leg, attempt)
        return response

    def test_cancelled_pickup_rebooks_with_history_new_reference_and_email(self):
        number = self.booked_pickup()
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R")), \
                patch.object(shipping.requests, "post", return_value=self.rebook_response(number)) as post:
            result = self.action(number, "rebook-shipment", leg="pickup", reference=number + "-R")
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["pickup_tracking_id"], "2234567890123")
        self.assertEqual(result.json["stage"], "awaiting_parcel")
        booking = result.json["shipping_bookings"]["pickup"]
        self.assertEqual(booking["reference"], number + "-R-2")
        self.assertEqual(booking["previous_bookings"][0]["waybill"], "1234567890123")
        self.assertEqual(ShippingBooking.query.count(), 2)
        self.assertEqual(json.loads(post.call_args.kwargs["data"]["data"])["shipments"][0]["order"], number + "-R-2")
        self.assertIsNotNone(Notification.query.filter_by(event_key=number + ":attempt2:pickup_booked").one().sent_at)
        self.assertNotIn("Shipment cancelled or returned — arrange next step", result.json["attention_reasons"])

    def test_closed_pickup_shows_cancelled_and_can_rebook(self):
        number = self.booked_pickup()
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R", "Closed", StatusType="CN")):
            result = self.action(number, "refresh-tracking")
            self.assertEqual(self.action(number, "refresh-tracking").status_code, 200)
        self.assertEqual(result.json["stage"], "pickup_cancelled")
        self.assertTrue(result.json["shipping_bookings"]["pickup"]["can_rebook"])
        self.assertEqual(Notification.query.filter_by(event_key=number + ":pickup_cancelled").count(), 1)
        listed = self.client.get("/api/admin/requests?stage=pickup_cancelled", headers=self.admin_headers).json
        self.assertEqual(listed["total"], 1)

    def test_rebooking_timeout_keeps_new_attempt_recoverable_and_fenced(self):
        number = self.booked_pickup()
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R")), \
                patch.object(shipping.requests, "post", side_effect=TimeoutError()) as post:
            result = self.action(number, "rebook-shipment", leg="pickup", reference=number + "-R")
            self.assertEqual(result.status_code, 409)
            self.assertEqual(self.action(number, "rebook-shipment", leg="pickup", reference=number + "-R").status_code, 400)
            self.assertEqual(self.action(number, "book-shipment", leg="pickup").status_code, 409)
            post.assert_called_once()
        self.assertIsNone(ReturnRequest.query.one().pickup_tracking_id)
        self.assertEqual(shipping.current_booking(number, "pickup").state, "uncertain")
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R-2", "Scheduled")):
            result = self.action(number, "reconcile-shipment", leg="pickup", waybill="2234567890123")
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["pickup_tracking_id"], "2234567890123")

    def test_rebook_does_not_duplicate_on_stale_click_or_active_tracking(self):
        number = self.booked_pickup()
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R", "Scheduled")), \
                patch.object(shipping.requests, "post") as post:
            self.assertEqual(self.action(number, "rebook-shipment", leg="pickup", reference=number + "-R").status_code, 409)
            post.assert_not_called()
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R")), \
                patch.object(shipping.requests, "post", return_value=self.rebook_response(number)):
            self.assertEqual(self.action(number, "rebook-shipment", leg="pickup", reference=number + "-R").status_code, 200)
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R-2")), \
                patch.object(shipping.requests, "post") as post:
            self.assertEqual(self.action(number, "rebook-shipment", leg="pickup", reference=number + "-R").status_code, 409)
            post.assert_not_called()
        self.assertEqual(ShippingBooking.query.count(), 2)

    def test_uncertain_attempt_can_reconcile_cancelled_waybill_then_rebook(self):
        number = self.submit().json["return_number"]
        with patch.object(shipping.requests, "post", side_effect=TimeoutError()):
            self.action(number, "accept-photos")
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R", "Closed")):
            result = self.action(number, "reconcile-shipment", leg="pickup", waybill="1234567890123")
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json["shipping_bookings"]["pickup"]["can_rebook"])
        self.assertIsNone(Notification.query.filter_by(event_key=number + ":pickup_booked").first())

    def test_rebooking_requires_admin_and_current_reference(self):
        number = self.booked_pickup()
        with patch.object(shipping.requests, "post") as post:
            self.assertEqual(self.action(number, "rebook-shipment", leg="pickup").status_code, 400)
            result = self.client.post(f"/api/admin/requests/return/{number}/rebook-shipment", headers=self.customer_headers,
                json={"leg": "pickup", "reference": number + "-R"})
            self.assertEqual(result.status_code, 401)
            post.assert_not_called()

    def test_returned_replacement_can_be_rebooked_and_delivered(self):
        number = self.submit("exchange").json["exchange_number"]
        with patch.object(shipping.requests, "post", side_effect=[self.response(number, "pickup"), self.response(number, "replacement", "3234567890123")]):
            self.action(number, "accept-photos", kind="exchange")
            self.action(number, "mark-parcel-received", kind="exchange")
            self.action(number, "accept-parcel", kind="exchange")
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-F", "Returned")), \
                patch.object(shipping.requests, "post", return_value=self.rebook_response(number, "replacement")):
            result = self.action(number, "rebook-shipment", kind="exchange", leg="replacement", reference=number + "-F")
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["outbound_tracking_id"], "2234567890123")
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-F-2", "Delivered", StatusType="DL")) as fetch:
            result = self.action(number, "refresh-tracking", kind="exchange")
            fetch.assert_called_once_with("2234567890123")
        self.assertEqual(result.json["stage"], "delivered")

    def test_pickup_dispatched_for_collection_can_cancel_but_collected_pending_cannot(self):
        number = self.booked_pickup()
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R", "Pending", StatusType="PU")), \
                patch.object(shipping.requests, "post") as post:
            self.assertEqual(self.action(number, "cancel-shipment", leg="pickup").status_code, 409)
            post.assert_not_called()
        response = Mock(status_code=200)
        response.json.return_value = {"status": True}
        with patch.object(shipping, "fetch_tracking", side_effect=[self.tracking(number + "-R", "Dispatched", StatusType="PP"), self.tracking(number + "-R")]), \
                patch.object(shipping.requests, "post", return_value=response) as post:
            self.assertEqual(self.action(number, "cancel-shipment", leg="pickup").status_code, 200)
            post.assert_called_once()

    def test_explicit_cancel_rejection_can_retry_and_does_not_confirm_cancellation(self):
        number = self.booked_pickup()
        response = Mock(status_code=200)
        response.json.return_value = {"status": False}
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R", "Scheduled")), \
                patch.object(shipping.requests, "post", return_value=response):
            self.assertEqual(self.action(number, "cancel-shipment", leg="pickup").status_code, 409)
        self.assertEqual(shipping.current_booking(number, "pickup").action_state, "rejected")
        self.assertFalse(shipping.booking_details(number)["pickup"]["can_rebook"])

    def test_old_scan_cannot_unlock_rebooking_after_uncertain_cancellation(self):
        number = self.booked_pickup()
        booking = shipping.current_booking(number, "pickup")
        booking.action = "cancel"
        booking.action_state = "uncertain"
        booking.action_requested_at = datetime.utcnow()
        db.session.commit()
        old = self.tracking(number + "-R", StatusDateTime=(datetime.utcnow() - timedelta(days=1)).isoformat() + "Z")
        with patch.object(shipping, "fetch_tracking", return_value=old), patch.object(shipping.requests, "post") as post:
            self.assertEqual(self.action(number, "rebook-shipment", leg="pickup", reference=number + "-R").status_code, 409)
            post.assert_not_called()

    def test_rejected_rebooking_can_retry_and_cancelled_attempt_can_rebook_again(self):
        number = self.booked_pickup()
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R")), \
                patch.object(shipping.requests, "post", return_value=Mock(status_code=401)):
            self.assertEqual(self.action(number, "rebook-shipment", leg="pickup", reference=number + "-R").status_code, 400)
        self.assertEqual(shipping.current_booking(number, "pickup").state, "not_attempted")
        with patch.object(shipping.requests, "post", return_value=self.rebook_response(number)):
            self.assertEqual(self.action(number, "book-shipment", leg="pickup").status_code, 200)
        response = self.rebook_response(number, attempt=3)
        response.json.return_value["packages"][0]["waybill"] = "3234567890123"
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R-2", "Closed")), \
                patch.object(shipping.requests, "post", return_value=response):
            result = self.action(number, "rebook-shipment", leg="pickup", reference=number + "-R-2")
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["shipping_bookings"]["pickup"]["attempt"], 3)
        self.assertEqual(len(result.json["shipping_bookings"]["pickup"]["previous_bookings"]), 2)
        self.assertIsNotNone(Notification.query.filter_by(event_key=number + ":attempt3:pickup_booked").one().sent_at)

    def test_cancel_suppresses_unsent_booking_confirmation(self):
        number = self.booked_pickup()
        original = Notification.query.filter_by(event_key=number + ":pickup_booked").one()
        original.sent_at = None
        db.session.commit()
        with patch.object(shipping, "fetch_tracking", return_value=self.tracking(number + "-R")):
            self.assertEqual(self.action(number, "refresh-tracking").status_code, 200)
        self.assertIsNone(Notification.query.filter_by(event_key=number + ":pickup_booked").first())
        self.assertIsNotNone(Notification.query.filter_by(event_key=number + ":pickup_cancelled").one().sent_at)

    def test_uncertain_replacement_cannot_be_marked_delivered(self):
        number = self.submit("exchange").json["exchange_number"]
        with patch.object(shipping.requests, "post", side_effect=[self.response(number, "pickup"), TimeoutError()]):
            self.action(number, "accept-photos", kind="exchange")
            self.action(number, "mark-parcel-received", kind="exchange")
            self.action(number, "accept-parcel", kind="exchange")
        result = self.action(number, "mark-delivered", kind="exchange")
        self.assertEqual(result.status_code, 409, result.json)
        self.assertIsNone(ExchangeRequest.query.one().delivered_at)
