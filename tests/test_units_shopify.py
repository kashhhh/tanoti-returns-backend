import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import test_workflows as fixtures
from app.extensions import db
from app.models import OrderItem, ReturnRequest, ExchangeRequest, RequestStatus
from app.services import shopify_returns as remote
from app.services.sync_service import upsert_order


def native_return(processed=0, exchange=False):
    return {"id":"gid://shopify/Return/55", "status":"OPEN" if not processed else "CLOSED",
        "returnLineItems":{"nodes":[{"id":"gid://shopify/ReturnLineItem/1", "quantity":1,
            "processedQuantity":processed, "processableQuantity":1-processed,
            "fulfillmentLineItem":{"id":"gid://shopify/FulfillmentLineItem/1", "lineItem":{"id":"gid://shopify/LineItem/1"}}}], "pageInfo":{"hasNextPage":False}},
        "exchangeLineItems":{"nodes":[{"id":"gid://shopify/ExchangeLineItem/1", "processedQuantity":processed,
            "quantity":1,"processableQuantity":1-processed,"lineItems":[{"id":"gid://shopify/LineItem/222"}]}] if exchange else [], "pageInfo":{"hasNextPage":False}},
        "reverseFulfillmentOrders":{"nodes":[{"lineItems":{"nodes":[{"id":"gid://shopify/ReverseFulfillmentOrderLineItem/1", "fulfillmentLineItem":{"id":"gid://shopify/FulfillmentLineItem/1"}}], "pageInfo":{"hasNextPage":False}}}], "pageInfo":{"hasNextPage":False}}}


class UnitAndShopifyTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    submit = fixtures.WorkflowTests.submit
    action = fixtures.WorkflowTests.action

    def test_rejection_blocks_both_request_types_for_same_unit(self):
        response = self.submit()
        number = response.json["return_number"]
        self.action(number,"reject-photos",rejection_reason="tags_removed")
        for kind in ("return","exchange"):
            self.assertEqual(self.submit(kind).status_code,400)
        item = self.client.get("/api/customer/orders", headers=self.customer_headers).json["expired"][0]
        self.assertFalse(item["eligible"])
        self.assertTrue(item["has_request"])

    def test_return_window_is_enforced_on_direct_submissions(self):
        item = db.session.get(OrderItem,self.item_id)
        for fulfilled in (None, datetime.utcnow()-timedelta(days=100)):
            item.order.fulfilled_at = fulfilled
            db.session.commit()
            for kind in ("return","exchange"):
                self.assertEqual(self.submit(kind).status_code,400)
        self.assertEqual(ReturnRequest.query.count(),0)
        self.assertEqual(ExchangeRequest.query.count(),0)

    def test_delivered_replacement_has_own_card_size_window_and_admin_tag(self):
        original_id = self.item_id
        number = self.submit("exchange").json["exchange_number"]
        req = ExchangeRequest.query.one()
        req.status = RequestStatus.COMPLETED
        req.replacement_line_id = "222"
        req.order_item.order.fulfilled_at = datetime.utcnow()-timedelta(days=100)
        db.session.commit()
        self.assertEqual(self.action(number,"mark-delivered",kind="exchange").status_code,200)
        self.action(number,"mark-delivered",kind="exchange")
        replacement = OrderItem.query.filter_by(replacement_for=number).one()
        self.assertNotEqual(replacement.id,original_id)
        self.assertEqual(replacement.size,"M")
        self.assertIsNone(replacement.request_block_reason(14))
        self.assertIsNotNone(db.session.get(OrderItem,original_id).request_block_reason(14))
        self.item_id = replacement.id
        response = self.submit()
        self.assertEqual(response.status_code,201,response.json)
        rows = self.client.get("/api/admin/requests",headers=self.admin_headers).json["requests"]
        self.assertEqual(next(r for r in rows if r["number"]==response.json["return_number"])["replacement_for"],number)

    def test_two_purchased_units_are_distinct_and_resync_is_idempotent(self):
        payload={"id":100,"order_number":100,"customer":{"id":1,"email":"customer@example.com"},
            "fulfillments":[{"created_at":datetime.utcnow().isoformat()+"Z","line_items":[{"id":1,"quantity":2}]}],
            "line_items":[{"id":1,"title":"Shirt","price":"999","quantity":2}]}
        upsert_order(payload)
        ids=[i.id for i in OrderItem.query.order_by(OrderItem.id)]
        upsert_order(payload)
        self.assertEqual(ids,[i.id for i in OrderItem.query.order_by(OrderItem.id)])
        self.assertEqual(len(ids),2)
        self.assertEqual(self.submit().status_code,201)
        self.item_id = ids[1]
        self.assertEqual(self.submit("exchange").status_code,201)
        self.assertEqual([i.unit_number for i in OrderItem.query.order_by(OrderItem.id)],[1,2])

    def test_unfulfilled_second_unit_does_not_inherit_first_unit_date(self):
        upsert_order({"id":100,"order_number":100,"customer":{"id":1,"email":"customer@example.com"},
            "fulfillments":[{"created_at":"2026-09-18T10:00:00+05:30","line_items":[{"id":1,"quantity":1}]}],
            "line_items":[{"id":1,"title":"Shirt","price":"999","quantity":2}]})
        items=OrderItem.query.order_by(OrderItem.unit_number).all()
        self.assertEqual(items[0].fulfilled_at,datetime(2026,9,18,4,30))
        self.assertIsNone(items[1].request_deadline(14))

    def test_failed_photo_save_releases_claim(self):
        with patch("app.services.storage_service.save_request_photos",side_effect=ValueError("Storage full")):
            self.assertEqual(self.submit().status_code,400)
        self.assertFalse(db.session.get(OrderItem,self.item_id).request_claimed)
        self.assertEqual(self.submit().status_code,201)

    def test_repeated_submission_does_not_create_second_request(self):
        self.assertEqual(self.submit().status_code,201)
        self.assertEqual(self.submit("exchange").status_code,400)
        self.assertEqual(ReturnRequest.query.count()+ExchangeRequest.query.count(),1)

    def make_request(self, kind="return"):
        self.submit(kind)
        obj=(ReturnRequest if kind=="return" else ExchangeRequest).query.one()
        self.app.config["SHOPIFY_STORE_DOMAIN"]="sample.myshopify.com"
        return obj

    def test_native_creation_is_one_unit_and_has_reconciliation_marker(self):
        req=self.make_request("exchange")
        req.status=RequestStatus.PICKUP_SCHEDULED
        db.session.commit()
        with patch.object(remote,"_reconcile",return_value=None), patch.object(remote,"_returnable",return_value=("gid://shopify/FulfillmentLineItem/1","gid://shopify/Location/1")), patch.object(remote,"mutation",return_value={"return":{"id":"gid://shopify/Return/55"}}) as call, patch.object(remote,"_read_return",return_value=native_return(exchange=True)), patch.object(remote,"_metadata"):
            remote.sync_one(req,"exchange")
        payload=call.call_args.args[1]["input"]
        self.assertEqual(payload["returnLineItems"][0]["quantity"],1)
        self.assertIn(req.exchange_number,payload["returnLineItems"][0]["returnReasonNote"])
        self.assertEqual(payload["exchangeLineItems"],[{"variantId":"gid://shopify/ProductVariant/200","quantity":1}])
        self.assertEqual(req.shopify_sync_status,"synced")

    def test_ambiguous_creation_is_not_blindly_retried(self):
        req=self.make_request()
        req.status=RequestStatus.PICKUP_SCHEDULED
        req.shopify_create_attempted=True
        db.session.commit()
        with patch.object(remote,"_reconcile",return_value=None),patch.object(remote,"mutation") as call:
            remote.sync_one(req,"return")
        call.assert_not_called()
        self.assertEqual(req.shopify_sync_status,"needs_review")
        with patch.object(remote,"_reconcile",return_value="gid://shopify/Return/55"), patch.object(remote,"_read_return",return_value=native_return()),patch.object(remote,"_metadata"),patch.object(remote,"mutation") as call:
            remote.sync_one(req,"return")
        call.assert_not_called()
        self.assertEqual(req.shopify_sync_status,"synced")

    def test_processing_records_restock_decision_without_moving_money(self):
        req=self.make_request()
        req.status=RequestStatus.COMPLETED
        req.shopify_return_id="gid://shopify/Return/55"
        req.restock=False
        db.session.commit()
        with patch.object(remote,"_read_return",side_effect=[native_return(),native_return(1)]),patch.object(remote,"_metadata"),patch.object(remote,"mutation",return_value={}) as call:
            remote.sync_one(req,"return")
        payload=call.call_args.args[1]["input"]
        self.assertNotIn("financialTransfer",payload)
        self.assertEqual(payload["returnLineItems"][0]["dispositions"][0]["dispositionType"],"NOT_RESTOCKED")
        with patch.object(remote,"_read_return",return_value=native_return(1)),patch.object(remote,"_metadata"),patch.object(remote,"mutation") as call:
            remote.sync_one(req,"return")
        call.assert_not_called()

    def test_exchange_processing_links_actual_replacement_line(self):
        req=self.make_request("exchange")
        req.status=RequestStatus.COMPLETED
        req.shopify_return_id="gid://shopify/Return/55"
        req.restock=False
        db.session.commit()
        with patch.object(remote,"_read_return",side_effect=[native_return(exchange=True),native_return(1,True)]),patch.object(remote,"_metadata"),patch.object(remote,"mutation",return_value={}):
            remote.sync_one(req,"exchange")
        self.assertEqual(req.replacement_line_id,"222")
        self.assertEqual(req.shopify_sync_status,"synced")

    def test_sync_failure_keeps_local_request_and_error_is_visible(self):
        req=self.make_request()
        with patch.object(remote,"_metadata",side_effect=RuntimeError("Missing write_orders scope")):
            remote.sync_one(req,"return")
        self.assertEqual(req.shopify_sync_status,"error")
        self.assertEqual(req.status,RequestStatus.PENDING)
        row=self.client.get("/api/admin/requests",headers=self.admin_headers).json["requests"][0]
        self.assertIn("write_orders",row["shopify_sync_error"])

    def test_early_shopify_order_webhook_is_reused_as_one_replacement_card(self):
        req=self.make_request("exchange")
        req.status=RequestStatus.COMPLETED
        req.replacement_line_id="222"
        req.delivered_at=datetime.utcnow()
        early=OrderItem(order_id=req.order_item.order_id,shopify_line_item_id="222",product_title="Shirt",price=999,quantity=1,unavailable=True)
        db.session.add(early);db.session.commit()
        from app.services.replacement_service import ensure_replacement
        replacement=ensure_replacement(req)
        db.session.commit()
        self.assertEqual(replacement.id,early.id)
        self.assertEqual(OrderItem.query.filter_by(shopify_line_item_id="222").count(),1)
        self.assertFalse(replacement.unavailable)

    def test_unknown_replacement_lines_stay_blocked_until_mapping(self):
        req=self.make_request("exchange")
        req.status=RequestStatus.COMPLETED
        db.session.commit()
        payload={"id":100,"order_number":100,"customer":{"id":1,"email":"customer@example.com"},
            "fulfillments":[{"created_at":datetime.utcnow().isoformat()+"Z","line_items":[{"id":222,"quantity":1}]}],
            "line_items":[{"id":1,"title":"Shirt","price":"999","quantity":1},{"id":222,"title":"Replacement","price":"999","quantity":1}]}
        upsert_order(payload);upsert_order(payload)
        self.assertTrue(OrderItem.query.filter_by(shopify_line_item_id="222").one().unavailable)

    def test_shopify_metadata_uses_request_key_and_never_contains_gift_code(self):
        req=self.make_request()
        req.gift_card_code="PRIVATE-CODE"
        with patch.object(remote,"mutation",return_value={}) as call:
            remote._metadata(req,"return")
        fields=call.call_args_list[0].args[1]["metafields"][0]
        self.assertEqual(fields["key"],req.return_number.lower().replace("-","_"))
        self.assertNotIn("PRIVATE-CODE",fields["value"])

    def test_pending_rejected_sync_does_not_create_native_return(self):
        req=self.make_request()
        req.status=RequestStatus.REJECTED
        db.session.commit()
        with patch.object(remote,"_metadata"),patch.object(remote,"_create") as create:
            remote.sync_one(req,"return")
        create.assert_not_called()
        self.assertEqual(req.shopify_sync_status,"synced")

    def test_variant_options_preserve_colour_and_named_size_position(self):
        from app.services.shopify_client import get_variants_for_product
        product={"options":[{"name":"Color","position":1},{"name":"Size","position":2}],
                 "variants":[{"id":1,"option1":"Red","option2":"S","inventory_quantity":1},
                             {"id":2,"option1":"Red","option2":"M","inventory_quantity":2},
                             {"id":3,"option1":"Blue","option2":"L","inventory_quantity":5}]}
        with patch("app.services.shopify_client._headers",return_value={}),patch("app.services.shopify_client.requests.get") as get:
            get.return_value.json.return_value={"product":product}
            rows=get_variants_for_product("100","1")
        self.assertEqual([r["size"] for r in rows],["S","M"])
