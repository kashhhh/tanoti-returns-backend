"""
Builds the step-by-step timeline shown to the customer on /my-requests --
one shared function for both ReturnRequest and ExchangeRequest, since the
two-gate flow (photo review -> pickup -> inspection -> outcome) is
identical; only the final step's label/fields differ.
"""
from app.services import shipping_service


def build_timeline(obj, kind: str):
    steps = [{
        "label": "Request submitted",
        "date": obj.created_at.isoformat(),
        "done": True,
    }]

    if obj.status == "rejected" and obj.rejected_stage == "photo":
        steps.append({
            "label": "Rejected",
            "date": obj.photo_decision_at.isoformat() if obj.photo_decision_at else None,
            "done": True,
            "note": obj.rejection_note or None,
        })
        return steps

    steps.append({
        "label": "Photos reviewed",
        "date": obj.photo_decision_at.isoformat() if obj.photo_decision_at else None,
        "done": obj.photo_decision_at is not None,
    })

    if obj.status == "pending":
        return steps

    steps.append({
        "label": "Pickup being arranged" if obj.pickup_carrier == "manual" else "Pickup scheduled",
        "date": obj.photo_decision_at.isoformat() if obj.photo_decision_at else None,
        "done": obj.pickup_carrier is not None,
        "tracking_id": obj.pickup_tracking_id,
        "tracking_url": shipping_service.tracking_url(obj.pickup_carrier, obj.pickup_tracking_id),
    })

    steps.append({
        "label": "Parcel received & being inspected",
        "date": obj.parcel_received_at.isoformat() if obj.parcel_received_at else None,
        "done": obj.parcel_received_at is not None,
    })

    if obj.status == "rejected" and obj.rejected_stage == "parcel":
        steps.append({
            "label": "Rejected after inspection",
            "date": obj.parcel_decision_at.isoformat() if obj.parcel_decision_at else None,
            "done": True,
            "note": obj.rejection_note or None,
        })
        return steps

    if kind == "return":
        steps.append({
            "label": "Gift card issued" if obj.refund_mode == "gift_card" else "Refund approved",
            "date": obj.completed_at.isoformat() if obj.completed_at else None,
            "done": obj.status == "completed",
        })
    else:
        steps.append({
            "label": "Replacement shipment booked" if obj.outbound_tracking_id else "Replacement being arranged",
            "date": obj.completed_at.isoformat() if obj.completed_at else None,
            "done": obj.status == "completed",
            "tracking_id": obj.outbound_tracking_id,
            "tracking_url": shipping_service.tracking_url(obj.outbound_carrier, obj.outbound_tracking_id),
        })

    return steps