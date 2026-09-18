"""
Database models for the Tanoti Returns app.

Design notes:
- Order / OrderItem are a local cache synced from Shopify (via webhook or
  periodic sync) so the app doesn't need to hit the Shopify Admin API on
  every page load, and so return-specific state (window, status) can live
  alongside the item.
- ReturnRequest and ExchangeRequest are separate tables, both pointing at
  an OrderItem, because their fields diverge (refund mode + photos vs.
  requested size) and an item can only ever have one *active* request of
  either kind at a time (enforced in application logic, not a DB
  constraint, so history is preserved).
- AdminSettings is a single-row table (key/value would also work, but a
  single settings row is simpler for the handful of values here).
"""
from datetime import datetime
from enum import Enum

from app.extensions import db


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PaymentMethod(str, Enum):
    PREPAID = "prepaid"
    COD = "cod"


class RequestStatus(str, Enum):
    PENDING = "pending"                    # awaiting photo review
    PICKUP_SCHEDULED = "pickup_scheduled"  # photos accepted; reverse pickup booked, awaiting parcel
    PARCEL_RECEIVED = "parcel_received"    # parcel arrived at the warehouse; awaiting physical inspection
    COMPLETED = "completed"                # legacy: parcel approved; outcome timestamps confirm final success
    REJECTED = "rejected"                  # rejected at either the photo or parcel stage (see rejected_stage)


class RefundMode(str, Enum):
    ACCOUNT = "account"              # manual transfer, owner-tracked
    GIFT_CARD = "gift_card"


class ReturnReason(str, Enum):
    SIZE_ISSUE = "size_issue"
    QUALITY_ISSUE = "quality_issue"
    WRONG_ITEM_RECEIVED = "wrong_item_received"
    DAMAGED = "damaged"
    NOT_AS_DESCRIBED = "not_as_described"
    CHANGED_MIND = "changed_mind"
    OTHER = "other"


class ExchangeReason(str, Enum):
    SIZE_TOO_SMALL = "size_too_small"
    SIZE_TOO_LARGE = "size_too_large"
    DAMAGED = "damaged"
    OTHER = "other"


class RejectionReason(str, Enum):
    TAGS_REMOVED = "tags_removed"
    ITEM_USED_WORN = "item_used_worn"
    OUTSIDE_RETURN_WINDOW = "outside_return_window"
    DAMAGE_NOT_MANUFACTURING = "damage_not_manufacturing"
    PHOTOS_INCONCLUSIVE = "photos_inconclusive"
    SUSPECTED_FRAUD = "suspected_fraud"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Core sync tables
# ---------------------------------------------------------------------------

class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    shopify_customer_id = db.Column(db.String(64), unique=True, index=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    orders = db.relationship("Order", back_populates="customer")


class Order(db.Model):
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    shopify_order_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    order_number = db.Column(db.String(32), nullable=False, index=True)  # what the customer searches by
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)

    payment_method = db.Column(db.Enum(PaymentMethod), nullable=False)
    financial_status = db.Column(db.String(32))       # paid / pending / refunded etc, from Shopify
    shipping_address = db.Column(db.JSON, nullable=True)

    fulfilled_at = db.Column(db.DateTime, nullable=True)   # return window clock starts here
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer", back_populates="orders")
    items = db.relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

    def return_window_deadline(self, window_days: int):
        if not self.fulfilled_at:
            return None
        from datetime import timedelta
        return self.fulfilled_at + timedelta(days=window_days)

    def is_within_return_window(self, window_days: int) -> bool:
        deadline = self.return_window_deadline(window_days)
        if deadline is None:
            return False
        return datetime.utcnow() <= deadline


class OrderItem(db.Model):
    __tablename__ = "order_items"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)

    shopify_line_item_id = db.Column(db.String(64), nullable=False)
    shopify_variant_id = db.Column(db.String(64))
    shopify_product_id = db.Column(db.String(64))

    product_title = db.Column(db.String(255), nullable=False)
    size = db.Column(db.String(32))
    sku = db.Column(db.String(64))
    quantity = db.Column(db.Integer, default=1)  # physical units per card: always one after migration
    unit_number = db.Column(db.Integer, nullable=False, default=1)
    line_quantity = db.Column(db.Integer, nullable=False, default=1)
    fulfilled_at = db.Column(db.DateTime, nullable=True)
    fulfillment_known = db.Column(db.Boolean, nullable=False, default=False)
    superseded = db.Column(db.Boolean, nullable=False, default=False)
    unavailable = db.Column(db.Boolean, nullable=False, default=False)
    request_claimed = db.Column(db.Boolean, nullable=False, default=False)
    replacement_for = db.Column(db.String(20), nullable=True, unique=True)
    shopify_fulfillment_line_id = db.Column(db.String(100), nullable=True)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    image_url = db.Column(db.String(512))

    order = db.relationship("Order", back_populates="items")
    return_requests = db.relationship("ReturnRequest", back_populates="order_item")
    exchange_requests = db.relationship("ExchangeRequest", back_populates="order_item")

    def request_deadline(self, days):
        from datetime import timedelta
        start = self.fulfilled_at if self.fulfillment_known else self.order.fulfilled_at
        return start + timedelta(days=days) if start else None

    def request_block_reason(self, days):
        if self.request_claimed or self.return_requests or self.exchange_requests:
            return "A request has already been submitted for this unit. See your request history."
        if self.unavailable or self.superseded:
            return "This unit is no longer available for return or exchange."
        deadline = self.request_deadline(days)
        if not deadline:
            return "This unit has not been fulfilled yet."
        if datetime.utcnow() > deadline:
            return "The return window for this unit has ended."
        return None

    def active_request(self):
        """Returns the current non-terminal request (return or exchange) on
        this item, if any -- used to decide whether the item still shows as
        eligible / clickable, or as 'pending'."""
        NON_TERMINAL = (RequestStatus.PENDING, RequestStatus.PICKUP_SCHEDULED, RequestStatus.PARCEL_RECEIVED)
        for r in self.return_requests:
            if r.status in NON_TERMINAL or (r.status == RequestStatus.COMPLETED and not r.gift_card_code and not r.refund_paid_at):
                return r
        for e in self.exchange_requests:
            if e.status in NON_TERMINAL or (e.status == RequestStatus.COMPLETED and not e.delivered_at):
                return e
        return None


# ---------------------------------------------------------------------------
# Request tables
# ---------------------------------------------------------------------------

class ReturnRequest(db.Model):
    __tablename__ = "return_requests"

    id = db.Column(db.Integer, primary_key=True)
    return_number = db.Column(db.String(20), unique=True, nullable=False, index=True)  # e.g. RET-000123

    order_item_id = db.Column(db.Integer, db.ForeignKey("order_items.id"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)

    reason = db.Column(db.Enum(ReturnReason), nullable=False)
    reason_other_text = db.Column(db.Text, nullable=True)
    customer_note = db.Column(db.Text, nullable=True)
    restock = db.Column(db.Boolean, nullable=True)
    shopify_location_id = db.Column(db.String(100), nullable=True)
    shopify_return_id = db.Column(db.String(100), nullable=True)
    shopify_sync_status = db.Column(db.String(32), nullable=False, default="pending")
    shopify_sync_error = db.Column(db.Text, nullable=True)
    shopify_sync_stage = db.Column(db.String(32), nullable=True)
    shopify_create_attempted = db.Column(db.Boolean, nullable=False, default=False)
    photo_urls = db.Column(db.JSON, default=list)   # min 2 (front/back)
    pickup_address = db.Column(db.JSON, nullable=True)

    refund_mode = db.Column(db.Enum(RefundMode), nullable=False)
    refund_amount = db.Column(db.Numeric(10, 2), nullable=False)       # item price
    deduction_applied = db.Column(db.Numeric(10, 2), default=0)        # from global toggle, snapshotted at submit time
    net_refund_amount = db.Column(db.Numeric(10, 2), nullable=False)   # refund_amount - deduction_applied

    refund_paid_at = db.Column(db.DateTime, nullable=True)
    refund_paid_by = db.Column(db.String(255), nullable=True)
    gift_card_code = db.Column(db.String(64), nullable=True)           # set once issued

    status = db.Column(db.Enum(RequestStatus), default=RequestStatus.PENDING, nullable=False)
    rejected_stage = db.Column(db.String(16), nullable=True)           # "photo" or "parcel"
    rejection_reason = db.Column(db.Enum(RejectionReason), nullable=True)
    rejection_note = db.Column(db.Text, nullable=True)

    # Reverse pickup -- booked once photos are accepted
    pickup_carrier = db.Column(db.String(32), nullable=True)           # "delhivery", "shipway", "manual"
    pickup_tracking_id = db.Column(db.String(64), nullable=True)
    pickup_status = db.Column(db.String(32), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    photo_decision_at = db.Column(db.DateTime, nullable=True)   # accepted/rejected at the photo gate
    parcel_received_at = db.Column(db.DateTime, nullable=True)  # owner marks parcel physically arrived
    parcel_decision_at = db.Column(db.DateTime, nullable=True)  # accepted/rejected after inspection
    completed_at = db.Column(db.DateTime, nullable=True)

    order_item = db.relationship("OrderItem", back_populates="return_requests")
    customer = db.relationship("Customer")


class ExchangeRequest(db.Model):
    __tablename__ = "exchange_requests"

    id = db.Column(db.Integer, primary_key=True)
    exchange_number = db.Column(db.String(20), unique=True, nullable=False, index=True)  # e.g. EXC-000123

    order_item_id = db.Column(db.Integer, db.ForeignKey("order_items.id"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)

    requested_size = db.Column(db.String(32), nullable=False)
    requested_variant_id = db.Column(db.String(64), nullable=True)
    replacement_line_id = db.Column(db.String(64), nullable=True)
    photo_urls = db.Column(db.JSON, default=list)
    pickup_address = db.Column(db.JSON, nullable=True)
    reason = db.Column(db.Enum(ExchangeReason), nullable=False)
    reason_other_text = db.Column(db.Text, nullable=True)
    customer_note = db.Column(db.Text, nullable=True)
    restock = db.Column(db.Boolean, nullable=True)
    shopify_location_id = db.Column(db.String(100), nullable=True)
    shopify_return_id = db.Column(db.String(100), nullable=True)
    shopify_sync_status = db.Column(db.String(32), nullable=False, default="pending")
    shopify_sync_error = db.Column(db.Text, nullable=True)
    shopify_sync_stage = db.Column(db.String(32), nullable=True)
    shopify_create_attempted = db.Column(db.Boolean, nullable=False, default=False)

    status = db.Column(db.Enum(RequestStatus), default=RequestStatus.PENDING, nullable=False)
    rejected_stage = db.Column(db.String(16), nullable=True)
    rejection_reason = db.Column(db.Enum(RejectionReason), nullable=True)
    rejection_note = db.Column(db.Text, nullable=True)

    # Reverse pickup of the original item -- booked once photos are accepted
    pickup_carrier = db.Column(db.String(32), nullable=True)
    pickup_tracking_id = db.Column(db.String(64), nullable=True)
    pickup_status = db.Column(db.String(32), nullable=True)

    # Forward shipment of the new (exchanged) item -- booked once the
    # returned parcel is inspected and accepted
    outbound_carrier = db.Column(db.String(32), nullable=True)
    outbound_tracking_id = db.Column(db.String(64), nullable=True)
    outbound_status = db.Column(db.String(32), nullable=True)
    delivered_at = db.Column(db.DateTime, nullable=True)
    delivered_by = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    photo_decision_at = db.Column(db.DateTime, nullable=True)
    parcel_received_at = db.Column(db.DateTime, nullable=True)
    parcel_decision_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    order_item = db.relationship("OrderItem", back_populates="exchange_requests")
    customer = db.relationship("Customer")


# ---------------------------------------------------------------------------
# Settings & auth
# ---------------------------------------------------------------------------

class AdminSettings(db.Model):
    """Single-row table. Use AdminSettings.get() to fetch/create it."""
    __tablename__ = "admin_settings"

    id = db.Column(db.Integer, primary_key=True)
    return_window_days = db.Column(db.Integer, nullable=False, default=14)
    deduction_enabled = db.Column(db.Boolean, nullable=False, default=False)
    deduction_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    @classmethod
    def get(cls):
        settings = cls.query.first()
        if not settings:
            settings = cls()
            db.session.add(settings)
            db.session.commit()
        return settings


class OTPToken(db.Model):
    __tablename__ = "otp_tokens"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    otp_hash = db.Column(db.String(128), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, default=0)
    resend_count = db.Column(db.Integer, default=0)
    consumed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AdminOTP(db.Model):
    __tablename__ = "admin_otps"
    email = db.Column(db.String(255), primary_key=True)
    otp_hash = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    consumed = db.Column(db.Boolean, nullable=False, default=False)


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    event_key = db.Column(db.String(100), nullable=False, unique=True)
    recipient = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    html = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    sent_at = db.Column(db.DateTime)
    attempts = db.Column(db.Integer, default=0, nullable=False)
