"""Presentation stages derived from recorded outcomes, never assumed payment/delivery."""
def stage_for(obj, kind):
    status = obj.status.value
    if status == "pickup_scheduled":
        return "awaiting_parcel" if obj.pickup_tracking_id else "pickup_pending"
    if status == "completed":
        if kind == "return":
            if obj.refund_paid_at:
                return "refund_paid"
            return "gift_card_issued" if obj.gift_card_code else "refund_approved"
        if obj.delivered_at:
            return "delivered"
        return "shipment_booked" if obj.outbound_tracking_id else "replacement_pending"
    return status


def summary_for(obj, kind):
    from app.services.shipping_service import tracking_url
    outbound = kind == "exchange" and obj.status.value == "completed"
    tracking = obj.outbound_tracking_id if outbound else obj.pickup_tracking_id
    carrier = obj.outbound_carrier if outbound else obj.pickup_carrier
    return {
        "stage": stage_for(obj, kind),
        "order_number": obj.order_item.order.order_number,
        "is_past": stage_for(obj, kind) in ("gift_card_issued", "refund_paid", "delivered", "rejected"),
        "outcome_at": (obj.refund_paid_at if kind == "return" else obj.delivered_at).isoformat() if (obj.refund_paid_at if kind == "return" else obj.delivered_at) else None,
        "tracking_id": tracking,
        "tracking_url": tracking_url(carrier, tracking),
    }
