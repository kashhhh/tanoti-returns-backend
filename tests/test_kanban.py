import unittest
from datetime import datetime

import test_workflows as fixtures
from app.extensions import db
from app.models import (Customer, ReturnRequest, ExchangeRequest, RequestStatus,
                        ReturnReason, ExchangeReason, RefundMode)


class KanbanTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown

    def add(self, number, kind="return", **values):
        common = dict(order_item_id=self.item_id, customer_id=Customer.query.one().id,
                      status=RequestStatus.PENDING, **values)
        if kind == "return":
            obj = ReturnRequest(return_number=number, reason=ReturnReason.SIZE_ISSUE,
                                refund_mode=RefundMode.GIFT_CARD, refund_amount=999,
                                net_refund_amount=999, **common)
        else:
            obj = ExchangeRequest(exchange_number=number, reason=ExchangeReason.SIZE_TOO_SMALL,
                                  requested_size="M", **common)
        db.session.add(obj)
        db.session.flush()
        return obj

    def column(self, name, **filters):
        response = self.client.get("/api/admin/requests", headers=self.admin_headers,
                                   query_string={"board_column": name, **filters})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_all_outcomes_appear_once_in_correct_columns(self):
        cases = [
            ("review", "pending", "return", RequestStatus.PENDING, {}),
            ("pickup", "pickup_pending", "exchange", RequestStatus.PICKUP_SCHEDULED, {}),
            ("pickup", "awaiting_parcel", "return", RequestStatus.PICKUP_SCHEDULED, {"pickup_tracking_id": "123"}),
            ("pickup", "pickup_cancelled", "return", RequestStatus.PICKUP_SCHEDULED, {"pickup_status": "Cancelled"}),
            ("inspection", "parcel_received", "return", RequestStatus.PARCEL_RECEIVED, {}),
            ("refund", "refund_approved", "return", RequestStatus.COMPLETED, {}),
            ("replacement", "replacement_pending", "exchange", RequestStatus.COMPLETED, {}),
            ("replacement", "shipment_booked", "exchange", RequestStatus.COMPLETED, {"outbound_tracking_id": "456"}),
            ("replacement", "replacement_cancelled", "exchange", RequestStatus.COMPLETED, {"outbound_status": "Returned"}),
            ("completed", "gift_card_issued", "return", RequestStatus.COMPLETED, {"gift_card_code": "TEST"}),
            ("completed", "refund_paid", "return", RequestStatus.COMPLETED, {"refund_paid_at": datetime.utcnow()}),
            ("completed", "delivered", "exchange", RequestStatus.COMPLETED, {"delivered_at": datetime.utcnow()}),
            ("completed", "no_refund_due", "return", RequestStatus.COMPLETED, {"net_refund_amount": 0}),
            ("rejected", "rejected", "return", RequestStatus.REJECTED, {}),
        ]
        expected = {}
        for index, (column, stage, kind, status, values) in enumerate(cases):
            obj = self.add(f"{'RET' if kind == 'return' else 'EXC'}-BOARD{index}", kind)
            obj.status = status
            for key, value in values.items():
                setattr(obj, key, value)
            expected.setdefault(column, set()).add(stage)
        db.session.commit()
        seen = []
        for column, stages in expected.items():
            result = self.column(column)
            self.assertEqual({row["stage"] for row in result["requests"]}, stages)
            self.assertEqual(result["total"], len(stages))
            seen.extend(row["number"] for row in result["requests"])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), len(cases))

    def test_independent_pagination_search_and_type_filters(self):
        for index in range(27):
            self.add(f"RET-BOARD{index:03}")
        pickup = self.add("EXC-PICKUP", "exchange")
        pickup.status = RequestStatus.PICKUP_SCHEDULED
        db.session.commit()
        first = self.column("review")
        second = self.column("review", page=2)
        self.assertEqual(first["total"], 27)
        self.assertEqual(len(first["requests"]), 25)
        self.assertEqual(len(second["requests"]), 2)
        self.assertFalse({row["number"] for row in first["requests"]} & {row["number"] for row in second["requests"]})
        self.assertEqual(self.column("pickup")["total"], 1)
        self.assertEqual(self.column("review", q="EXC-PICKUP")["total"], 0)
        self.assertEqual(self.column("pickup", q="EXC-PICKUP")["total"], 1)
        self.assertEqual(self.column("pickup", type="return")["total"], 0)
        self.assertEqual(self.column("review", q="customer@example.com")["total"], 27)
        self.assertEqual(self.column("review", **{"from": "2099-01-01"})["total"], 0)

    def test_invalid_column_and_conflicting_stage_are_rejected(self):
        for query in ({"board_column": "missing"}, {"board_column": "review", "stage": "rejected"}):
            response = self.client.get("/api/admin/requests", query_string=query, headers=self.admin_headers)
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
