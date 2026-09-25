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
