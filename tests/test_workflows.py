import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from app import create_app
from app.config import Config
from app.extensions import db
from app.models import (Customer, Order, OrderItem, PaymentMethod, AdminOTP,
                        ReturnRequest, ExchangeRequest, Notification, RequestStatus)
from app.utils.sessions import create_session, cookie_name, csrf_token
from app.services.notification_service import deliver_pending
from app.services.storage_service import cleanup_old_photos

ADDRESS = dict(name="Customer", phone="9876543210", address1="10 Original Road", address2="",
               city="Mumbai", province="Maharashtra", zip="400001", country="India")


def photo():
    stream = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(stream, "JPEG")
    stream.seek(0)
    return stream, "item.jpg"


def session_headers(role, subject, email):
    raw = create_session(role, subject, email)
    db.session.commit()
    return {"Cookie": f"{cookie_name(role)}={raw}", "X-CSRF-Token": csrf_token(raw)}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        class TestConfig(Config):
            TESTING = True
            SESSION_COOKIE_SECURE = False
            TESTING_MODE = True
            SECRET_KEY = "test-signing-key"
            ADMIN_SECRET_KEY = "test-admin-secret"
            ADMIN_EMAILS = ["staff@example.com"]
            SQLALCHEMY_DATABASE_URI = "sqlite://"
            RESEND_API_KEY = None
        self.app = create_app(TestConfig)
        self.app.config["PHOTOS_STORAGE_DIR"] = self.directory.name
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        customer = Customer(email="customer@example.com", name="Customer")
        order = Order(shopify_order_id="100", order_number="100", customer=customer,
                      payment_method=PaymentMethod.PREPAID, shipping_address=ADDRESS,
                      fulfilled_at=datetime.utcnow())
        item = OrderItem(order=order, shopify_line_item_id="1", product_title="Shirt <b>red</b>", price=999, size="S")
        db.session.add(item); db.session.commit()
        self.item_id = item.id
        self.customer_headers = session_headers("customer", customer.id, customer.email)
        self.admin_headers = session_headers("admin", "staff@example.com", "staff@example.com")
        self.client = self.app.test_client(use_cookies=False)
        self.network = patch("requests.post", side_effect=AssertionError("Unexpected live network call"))
        self.network.start()
        self.stock = patch("app.services.shopify_client.select_exchange_variant", return_value={"id":"200", "size":"M"})
        self.stock.start()

    def tearDown(self):
        self.stock.stop()
        self.network.stop()
        db.session.remove(); db.drop_all(); self.ctx.pop(); self.directory.cleanup()

    def submit(self, kind="return", **changes):
        data = dict(reason="size_issue" if kind == "return" else "size_too_small", size="M",
                    refund_mode="gift_card", photo_front=photo(), photo_back=photo(),
                    pickup_address=json.dumps({**ADDRESS, "address1": "20 Pickup Road"}))
        data.update(changes)
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.post(f"/api/customer/order-items/{self.item_id}/{kind}",
                                data=data, headers=self.customer_headers)

    def action(self, number, action, kind="return", **data):
        if action == "accept-parcel":
            data.setdefault("restock", False)
        return self.client.post(f"/api/admin/requests/{kind}/{number}/{action}",
                                json=data, headers=self.admin_headers)

    def test_admin_requires_secret_and_allowlist(self):
        with patch("app.auth.admin_routes.send_admin_otp_email") as send:
            for email, secret in [("stranger@example.com", "test-admin-secret"), ("staff@example.com", "bad")]:
                self.assertEqual(self.client.post("/api/admin/auth/request-otp", json=dict(email=email, secret=secret)).status_code, 401)
            send.assert_not_called()
        self.assertEqual(self.client.get("/api/admin/settings", headers={"X-Admin-Key": "test-admin-secret"}).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/settings", headers=self.customer_headers).status_code, 401)
        self.assertEqual(self.client.get("/api/customer/orders", headers=self.admin_headers).status_code, 401)

    def test_otp_cooldown_single_use_and_session(self):
        payload = dict(email="STAFF@example.com", secret="test-admin-secret")
        with patch("app.auth.admin_routes.send_admin_otp_email") as send:
            self.assertEqual(self.client.post("/api/admin/auth/request-otp", json=payload).status_code, 200)
            code = send.call_args.args[1]
            self.assertEqual(self.client.post("/api/admin/auth/request-otp", json=payload).status_code, 429)
            payload["otp"] = code
            result = self.client.post("/api/admin/auth/verify-otp", json=payload)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.client.get("/api/admin/settings", headers={"Cookie": result.headers["Set-Cookie"].split(";", 1)[0]}).status_code, 200)
            self.assertEqual(self.client.post("/api/admin/auth/verify-otp", json=payload).status_code, 400)

    def test_otp_expiry_attempts_and_resend(self):
        payload = dict(email="staff@example.com", secret="test-admin-secret")
        with patch("app.auth.admin_routes.send_admin_otp_email") as send:
            self.client.post("/api/admin/auth/request-otp", json=payload)
            old_code = send.call_args.args[1]
            wrong = "000000" if old_code != "000000" else "111111"
            for _ in range(5):
                self.assertEqual(self.client.post("/api/admin/auth/verify-otp", json={**payload, "otp": wrong}).status_code, 400)
            self.assertEqual(self.client.post("/api/admin/auth/verify-otp", json={**payload, "otp": old_code}).status_code, 429)
            token = db.session.get(AdminOTP, payload["email"])
            token.created_at -= timedelta(minutes=1); db.session.commit()
            self.assertEqual(self.client.post("/api/admin/auth/resend-otp", json=payload).status_code, 200)
            code = send.call_args.args[1]
            token.expires_at = datetime.utcnow()-timedelta(seconds=1); db.session.commit()
            self.assertEqual(self.client.post("/api/admin/auth/verify-otp", json={**payload, "otp": code}).status_code, 400)

    def test_expired_or_removed_admin_cannot_access(self):
        self.app.config["ADMIN_EMAILS"] = []
        self.assertEqual(self.client.get("/api/admin/settings", headers=self.admin_headers).status_code, 401)

    def test_return_address_snapshot_and_admin_photos(self):
        result = self.submit()
        self.assertEqual(result.status_code, 201, result.json)
        req = ReturnRequest.query.one()
        self.assertEqual(req.pickup_address["address1"], "20 Pickup Road")
        self.assertEqual(req.order_item.order.shipping_address["address1"], "10 Original Road")
        self.assertEqual(len(req.photo_urls), 2)
        detail = self.client.get("/api/admin/requests", headers=self.admin_headers).json["requests"][0]
        self.assertEqual(detail["pickup_address"], req.pickup_address)
        self.assertEqual(len(detail["photo_urls"]), 2)
        self.assertIn("&lt;b&gt;", Notification.query.one().html)

    def test_exchange_photos_and_default_address(self):
        result = self.submit("exchange", pickup_address=None)
        self.assertEqual(result.status_code, 201, result.json)
        req = ExchangeRequest.query.one()
        self.assertEqual(req.pickup_address, ADDRESS)
        self.assertEqual(len(req.photo_urls), 2)
        self.assertEqual(len(self.client.get("/api/admin/requests", headers=self.admin_headers).json["requests"][0]["photo_urls"]), 2)

    def test_missing_or_invalid_photo_rejected_for_both(self):
        for kind in ("return", "exchange"):
            self.assertEqual(self.submit(kind, photo_back=None).status_code, 400)
            self.assertEqual(self.submit(kind, photo_back=(io.BytesIO(b"not an image"), "bad.jpg")).status_code, 400)
            self.assertEqual(self.submit(kind, photo_front=[photo(), photo()]).status_code, 400)
        self.assertEqual(ReturnRequest.query.count()+ExchangeRequest.query.count(), 0)
        self.assertEqual(list(Path(self.directory.name).rglob("*.jpg")), [])

    def test_incomplete_address_rejected(self):
        self.assertEqual(self.submit(pickup_address=json.dumps({"name": "Only name"})).status_code, 400)
        self.assertEqual(self.submit(pickup_address="not json").status_code, 400)

    def test_cannot_submit_for_another_customer(self):
        other = Customer(email="other@example.com"); db.session.add(other); db.session.commit()
        self.customer_headers = session_headers("customer", other.id, other.email)
        self.assertEqual(self.submit().status_code, 404)

    def test_gift_card_flow_emails_and_pickup_address(self):
        number = self.submit().json["return_number"]
        with patch("app.services.shipping_service.schedule_reverse_pickup", return_value=("delhivery", "TRACK", "scheduled")) as pickup:
            self.assertEqual(self.action(number, "accept-photos").status_code, 200)
            self.assertEqual(pickup.call_args.kwargs["pickup_address"]["address1"], "20 Pickup Road")
        self.assertEqual(self.action(number, "mark-parcel-received").status_code, 200)
        with patch("app.services.shopify_client.issue_gift_card", side_effect=lambda **kw: {"id": 567, "initial_value": kw["amount"], "currency": "INR", "note": kw["note"], "code": kw["code"]}):
            self.assertEqual(self.action(number, "accept-parcel").status_code, 200)
        self.assertEqual(Notification.query.count(), 3)
        self.assertIn(ReturnRequest.query.filter_by(return_number=number).one().gift_card_code, Notification.query.filter_by(event_key=number+":gift_card").one().html)
        self.assertEqual(self.action(number, "accept-parcel").status_code, 400)
        self.assertEqual(Notification.query.count(), 3)

    def test_manual_fallback_emails_do_not_claim_booking_or_payment(self):
        number = self.submit(refund_mode="account").json["return_number"]
        self.assertEqual(self.action(number, "accept-photos").status_code, 200)
        self.assertIsNone(Notification.query.filter_by(event_key=number+":pickup_pending").first())
        self.action(number, "mark-parcel-received")
        self.assertEqual(self.action(number, "accept-parcel").status_code, 200)
        self.assertIsNone(Notification.query.filter_by(event_key=number+":refund_approved").first())

    def test_rejection_reason_escaped(self):
        number = self.submit().json["return_number"]
        self.assertEqual(self.action(number, "reject-photos", rejection_reason="other", note="<script>bad</script>").status_code, 200)
        self.assertIsNone(Notification.query.filter_by(event_key=number+":rejected").first())
        html = Notification.query.filter_by(event_key=number+":received").one().html
        self.assertIn("&lt;b&gt;red&lt;/b&gt;", html)

    def test_email_failure_does_not_fail_request_and_can_retry(self):
        self.app.config.update(RESEND_API_KEY="fake", TESTING_MODE=False)
        result = self.submit()
        self.assertEqual(result.status_code, 201)
        self.assertIsNone(Notification.query.one().sent_at)
        with patch("requests.post") as send:
            deliver_pending()
            self.assertIsNotNone(Notification.query.one().sent_at)
            deliver_pending()
            self.assertEqual(send.call_count, 1)
            self.assertIn("Idempotency-Key", send.call_args.kwargs["headers"])

    def test_exchange_retention_and_quota(self):
        self.submit("exchange")
        req = ExchangeRequest.query.one()
        req.created_at = datetime.utcnow()-timedelta(days=121); db.session.commit()
        cleanup_old_photos()
        self.assertEqual(req.photo_urls, [])
        self.assertEqual(list(Path(self.directory.name).rglob("*.jpg")), [])

    def test_full_storage_rejects_upload_without_orphan_photos(self):
        self.app.config["MAX_PHOTO_STORAGE_MB"] = 0
        self.assertEqual(self.submit().status_code, 400)
        self.assertEqual(list(Path(self.directory.name).rglob("*.jpg")), [])
        self.assertEqual(ReturnRequest.query.count(), 0)

    def test_exchange_completion_emails(self):
        number = self.submit("exchange").json["exchange_number"]
        self.action(number, "accept-photos", kind="exchange")
        self.action(number, "mark-parcel-received", kind="exchange")
        with patch("app.services.shipping_service.schedule_forward_shipment", return_value=("delhivery", "OUTBOUND", "scheduled")):
            self.assertEqual(self.action(number, "accept-parcel", kind="exchange").status_code, 200)
        message = Notification.query.filter_by(event_key=number+":replacement_booked").one()
        self.assertIn("OUTBOUND", message.html)
        self.assertIn("arranged", message.subject)

    def test_sync_does_not_overwrite_pickup_snapshot(self):
        from app.services.sync_service import upsert_order
        self.submit()
        upsert_order({"id": 100, "order_number": 100, "customer": {"id": 1, "email": "customer@example.com"},
                      "shipping_address": {**ADDRESS, "address1": "New shipping address"}})
        req = ReturnRequest.query.one()
        self.assertEqual(req.pickup_address["address1"], "20 Pickup Road")
        self.assertEqual(req.order_item.order.shipping_address["address1"], "New shipping address")

    def test_shared_testing_flag_admin_console_without_resend(self):
        self.app.config.update(TESTING_MODE=True, DEBUG=False, RESEND_API_KEY="fake")
        payload = dict(email="staff@example.com", secret="test-admin-secret")
        with patch("resend.Emails.send") as send, patch("builtins.print") as output:
            result = self.client.post("/api/admin/auth/request-otp", json=payload)
        send.assert_not_called()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["delivery"], "console")
        code = output.call_args.args[0].rsplit(": ", 1)[1]
        self.assertNotIn(code, result.get_data(as_text=True))
        self.assertEqual(self.client.post("/api/admin/auth/verify-otp", json={**payload, "otp": code}).status_code, 200)

    def test_shared_testing_flag_customer_console_and_verification(self):
        self.app.config.update(TESTING_MODE=True, DEBUG=False, RESEND_API_KEY="fake")
        payload = dict(email="customer@example.com")
        with patch("resend.Emails.send") as send, patch("builtins.print") as output, patch("app.services.shopify_client.find_customer_by_email", return_value={"id": "100"}):
            result = self.client.post("/api/auth/request-otp", json=payload)
        send.assert_not_called()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["delivery"], "console")
        code = output.call_args.args[0].rsplit(": ", 1)[1]
        self.assertNotIn(code, result.get_data(as_text=True))
        self.assertEqual(self.client.post("/api/auth/verify-otp", json={**payload, "otp": code}).status_code, 200)

    def test_production_email_failures_do_not_log_codes_even_in_debug(self):
        self.app.config.update(TESTING_MODE=False, DEBUG=True, RESEND_API_KEY="fake")
        with patch("resend.Emails.send", side_effect=RuntimeError("unverified domain")), patch("builtins.print") as output, patch("app.services.shopify_client.find_customer_by_email", return_value={"id": "100"}):
            admin = self.client.post("/api/admin/auth/request-otp", json=dict(email="staff@example.com", secret="test-admin-secret"))
            customer = self.client.post("/api/auth/request-otp", json=dict(email="customer@example.com"))
        self.assertEqual(admin.status_code, 503)
        self.assertEqual(customer.status_code, 503)
        output.assert_not_called()

    def test_testing_mode_does_not_send_status_emails(self):
        self.app.config.update(TESTING_MODE=True, RESEND_API_KEY="fake")
        with patch("requests.post") as send:
            self.assertEqual(self.submit().status_code, 201)
        send.assert_not_called()
    def test_testing_mode_does_not_bypass_webhook_signature(self):
        self.app.config.update(TESTING_MODE=True, SHOPIFY_WEBHOOK_SECRET=None)
        result = self.client.post("/api/webhooks/shopify/orders-create", json={})
        self.assertEqual(result.status_code, 401)

    def test_testing_mode_still_uses_shopify_customer_lookup(self):
        from app.services.shopify_client import find_customer_by_email
        self.app.config["TESTING_MODE"] = True
        with patch("app.services.shopify_client._headers", return_value={}), patch("requests.get") as get:
            get.return_value.json.return_value = {"customers": [{"id": 42, "email": "customer@example.com"}]}
            self.assertEqual(find_customer_by_email("customer@example.com"), {"id": 42, "email": "customer@example.com"})
            get.assert_called_once()

    def seed_listing(self):
        from app.models import ReturnReason, ExchangeReason, RefundMode
        item = db.session.get(OrderItem, self.item_id)
        for i in range(37):
            fields = dict(order_item=item, customer=item.order.customer, created_at=datetime(2026, 8, 1) + timedelta(days=i//2), status=RequestStatus.PENDING)
            if i % 2:
                obj = ExchangeRequest(exchange_number=f"EXC-{i:06d}", requested_size="M", reason=ExchangeReason.SIZE_TOO_SMALL, **fields)
            else:
                obj = ReturnRequest(return_number=f"RET-{i:06d}", reason=ReturnReason.SIZE_ISSUE, refund_mode=RefundMode.ACCOUNT, refund_amount=999, net_refund_amount=999, **fields)
            db.session.add(obj)
        db.session.commit()

    def test_admin_pagination_combines_types_without_duplicates(self):
        self.seed_listing()
        first = self.client.get("/api/admin/requests?stage=pending&per_page=25", headers=self.admin_headers).json
        second = self.client.get("/api/admin/requests?stage=pending&per_page=25&page=2", headers=self.admin_headers).json
        self.assertEqual(first["total"], 37)
        self.assertEqual(first["pages"], 2)
        self.assertEqual(len(first["requests"]), 25)
        self.assertEqual(len(second["requests"]), 12)
        numbers = [r["number"] for r in first["requests"]+second["requests"]]
        self.assertEqual(len(set(numbers)), 37)
        dates = [r["date"] for r in first["requests"]+second["requests"]]
        self.assertEqual(dates, sorted(dates))
        clamped = self.client.get("/api/admin/requests?page=999", headers=self.admin_headers).json
        self.assertEqual(clamped["page"], 2)

    def test_admin_search_type_date_and_stage_counts(self):
        self.seed_listing()
        result = self.client.get("/api/admin/requests?type=exchange&from=2026-08-01&to=2026-08-02&q=customer@example.com", headers=self.admin_headers).json
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["counts"]["pending"], 2)
        self.assertEqual(result["counts"]["all"], 2)
        self.assertTrue(all(r["type"] == "exchange" for r in result["requests"]))
        result = self.client.get("/api/admin/requests?q=EXC-000001", headers=self.admin_headers).json
        self.assertEqual(result["total"], 1)
        result = self.client.get("/api/admin/requests?q=%25", headers=self.admin_headers).json
        self.assertEqual(result["total"], 0)

    def test_admin_filters_distinguish_manual_and_confirmed_outcomes(self):
        self.seed_listing()
        returns = ReturnRequest.query.order_by(ReturnRequest.id).all()
        returns[0].status = RequestStatus.PICKUP_SCHEDULED
        returns[1].status = RequestStatus.PICKUP_SCHEDULED
        returns[1].pickup_tracking_id = "PICKUP-123"
        returns[2].status = RequestStatus.COMPLETED
        returns[3].status = RequestStatus.COMPLETED
        returns[3].gift_card_code = "GIFT"
        exchanges = ExchangeRequest.query.order_by(ExchangeRequest.id).all()
        exchanges[0].status = RequestStatus.COMPLETED
        exchanges[1].status = RequestStatus.COMPLETED
        exchanges[1].outbound_tracking_id = "SHIPMENT"
        db.session.commit()
        for stage in ("pickup_pending", "awaiting_parcel", "refund_approved", "gift_card_issued", "replacement_pending", "shipment_booked"):
            result = self.client.get("/api/admin/requests?stage="+stage, headers=self.admin_headers).json
            self.assertEqual(result["total"], 1, stage)
            self.assertEqual(result["requests"][0]["stage"], stage)
            self.assertEqual(result["counts"]["all"], 37)
        customer = self.client.get("/api/customer/my-requests", headers=self.customer_headers).json["requests"]
        self.assertFalse(next(r for r in customer if r["stage"] == "refund_approved")["is_past"])
        self.assertFalse(next(r for r in customer if r["stage"] == "awaiting_parcel")["is_past"])

    def test_manual_outcomes_are_authorized_idempotent_and_move_to_history(self):
        self.seed_listing()
        ret = ReturnRequest.query.first()
        exc = ExchangeRequest.query.first()
        for obj in (ret, exc):
            obj.status = RequestStatus.COMPLETED
        db.session.commit()
        for obj, kind, number, action, field, stage in (
            (ret, "return", ret.return_number, "mark-refund-paid", "refund_paid_at", "refund_paid"),
            (exc, "exchange", exc.exchange_number, "mark-delivered", "delivered_at", "delivered")):
            url = f"/api/admin/requests/{kind}/{number}/{action}"
            self.assertEqual(self.client.post(url, headers=self.customer_headers).status_code, 401)
            first = self.action(number, action, kind=kind)
            self.assertEqual(first.status_code, 200, first.json)
            self.assertEqual(first.json["stage"], stage)
            timestamp = first.json[field]
            self.assertEqual(self.action(number, action, kind=kind).json[field], timestamp)
            self.assertEqual(Notification.query.filter_by(event_key=number+":"+stage).count(), 1)
            self.assertEqual(first.json[field.replace("_at", "_by")], "staff@example.com")
        rows = self.client.get("/api/customer/my-requests", headers=self.customer_headers).json["requests"]
        self.assertTrue(all(r["is_past"] for r in rows if r["number"] in (ret.return_number, exc.exchange_number)))

    def test_cannot_confirm_unapproved_or_gift_card_refund(self):
        self.seed_listing()
        ret = ReturnRequest.query.first()
        exc = ExchangeRequest.query.first()
        self.assertEqual(self.action(ret.return_number, "mark-refund-paid").status_code, 400)
        self.assertEqual(self.action(exc.exchange_number, "mark-delivered", kind="exchange").status_code, 400)
        ret.status = RequestStatus.COMPLETED
        ret.gift_card_code = "ISSUED"
        db.session.commit()
        self.assertEqual(self.action(ret.return_number, "mark-refund-paid").status_code, 400)

    def test_analytics_counts_finances_and_drilldown(self):
        self.seed_listing()
        returns = ReturnRequest.query.order_by(ReturnRequest.id).all()
        for ret in returns[:3]:
            ret.status = RequestStatus.COMPLETED
        returns[1].refund_paid_at = datetime.utcnow()
        returns[2].gift_card_code = "ISSUED"
        db.session.commit()
        result = self.client.get("/api/admin/analytics/overview?from=2026-08-01&to=2026-08-31", headers=self.admin_headers)
        self.assertEqual(result.status_code, 200, result.json)
        data = result.json
        self.assertEqual((data["total"], data["active"], data["successful"]), (37,35,2))
        self.assertEqual(data["finance"], dict(approved_unpaid="999.00",paid="999.00",gift_cards="999.00"))
        self.assertEqual(sum(r["count"] for r in data["trend"]), 37)
        self.assertEqual(len(data["trend"]), 31)
        product = data["products"][0]
        rows = self.client.get("/api/admin/requests", query_string={"product_key":product["product_key"], "size":product["size"], "reason":product["reason"], "type":product["kind"]}, headers=self.admin_headers).json
        self.assertEqual(rows["total"], product["count"])
        filtered = self.client.get("/api/admin/analytics/overview?from=2026-08-01&to=2026-08-02&type=exchange", headers=self.admin_headers).json
        self.assertEqual(filtered["total"], 2)
        self.assertEqual(filtered["finance"]["paid"], "0.00")

    def test_analytics_empty_invalid_and_previous_period(self):
        self.seed_listing()
        result = self.client.get("/api/admin/analytics/overview?from=2026-09-01&to=2026-09-30", headers=self.admin_headers).json
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["previous_total"], 35)
        self.assertEqual(result["oldest"], [])
        for query in ("from=bad", "type=bad", "from=2020-01-01&to=2026-01-01", "from=2026-09-02&to=2026-09-01"):
            self.assertEqual(self.client.get("/api/admin/analytics/overview?"+query, headers=self.admin_headers).status_code, 400)

    def test_customer_notes_saved_and_visible_for_both_request_types(self):
        for kind, model in (("return", ReturnRequest), ("exchange", ExchangeRequest)):
            with self.subTest(kind=kind):
                note = "Please inspect the seam.\n<script>alert('feedback')</script>"
                response = self.submit(kind, customer_note="  " + note + "  ")
                self.assertEqual(response.status_code, 201, response.json)
                req = model.query.one()
                self.assertEqual(req.customer_note, note)
                number = response.json[kind+"_number"]
                admin = self.client.get("/api/admin/requests", headers=self.admin_headers).json["requests"]
                self.assertEqual(next(r for r in admin if r["number"] == number)["customer_note"], note)
                customer = self.client.get("/api/customer/my-requests", headers=self.customer_headers).json["requests"]
                self.assertEqual(next(r for r in customer if r["number"] == number)["customer_note"], note)
                # A new physical unit is required; rejection no longer reopens the original.
                original = db.session.get(OrderItem, self.item_id)
                fresh = OrderItem(order_id=original.order_id, shopify_line_item_id="note-test-next", product_title=original.product_title, price=999, size="S")
                db.session.add(fresh)
                db.session.commit()
                self.item_id = fresh.id

    def test_customer_notes_optional_and_length_checked_before_upload(self):
        for kind, model in (("return", ReturnRequest), ("exchange", ExchangeRequest)):
            with self.subTest(kind=kind):
                response = self.submit(kind, customer_note="x" * 2001)
                self.assertEqual(response.status_code, 400)
                self.assertIn("2,000", response.json["error"])
                self.assertEqual(model.query.count(), 0)
                self.assertEqual(list(Path(self.directory.name).rglob("*.jpg")), [])
        response = self.submit(customer_note="   ")
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(ReturnRequest.query.one().customer_note)

    def test_customer_note_exact_limit_and_other_reason_are_independent(self):
        response = self.submit(customer_note="x" * 2000, reason="other", reason_other_text="A different reason")
        self.assertEqual(response.status_code, 201)
        req = ReturnRequest.query.one()
        self.assertEqual(req.customer_note, "x" * 2000)
        self.assertEqual(req.reason_other_text, "A different reason")

    def test_admin_invalid_pagination_and_dates(self):
        for query in ("page=0", "page=no", "per_page=10000", "stage=invalid", "from=wrong", "from=2026-09-01&to=2026-08-01"):
            self.assertEqual(self.client.get("/api/admin/requests?"+query, headers=self.admin_headers).status_code, 400)



if __name__ == "__main__":
    unittest.main()
