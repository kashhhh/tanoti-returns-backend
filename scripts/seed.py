"""
Seeds local test data so you can exercise the full return/exchange flow
for database-only development. Customer login still validates against the live Shopify store.

Usage:
    python scripts/seed.py
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app import create_app
from app.extensions import db
from app.models import Customer, Order, OrderItem, PaymentMethod

TEST_EMAIL = "test@example.com"

app = create_app()

with app.app_context():
    customer = Customer.query.filter_by(email=TEST_EMAIL).first()
    if not customer:
        customer = Customer(email=TEST_EMAIL, shopify_customer_id="mock-1000", name="Test Customer")
        db.session.add(customer)
        db.session.commit()
        print(f"Created customer: {customer.email}")
    else:
        print(f"Customer already exists: {customer.email}")

    # Order 1: within return window, prepaid, two items
    order1 = Order.query.filter_by(shopify_order_id="mock-order-1").first()
    if not order1:
        order1 = Order(
            shopify_order_id="mock-order-1",
            order_number="1001",
            customer_id=customer.id,
            payment_method=PaymentMethod.PREPAID,
            financial_status="paid",
            fulfilled_at=datetime.utcnow() - timedelta(days=3),  # 3 days ago -> within a 14-day window
        )
        db.session.add(order1)
        db.session.flush()
        db.session.add_all([
            OrderItem(
                order_id=order1.id, shopify_line_item_id="li-1", shopify_product_id="mock-prod-1",
                product_title="Block Print Kurta - Indigo", size="M", sku="KUR-IND-M",
                quantity=1, price=2499.00, image_url=None,
            ),
            OrderItem(
                order_id=order1.id, shopify_line_item_id="li-2", shopify_product_id="mock-prod-2",
                product_title="Hand Block Dupatta - Rust", size="Free Size", sku="DUP-RUST-FS",
                quantity=1, price=1299.00, image_url=None,
            ),
        ])
        print("Created order 1001 (within return window, prepaid, 2 items)")

    # Order 2: outside return window, COD, one item
    order2 = Order.query.filter_by(shopify_order_id="mock-order-2").first()
    if not order2:
        order2 = Order(
            shopify_order_id="mock-order-2",
            order_number="0987",
            customer_id=customer.id,
            payment_method=PaymentMethod.COD,
            financial_status="pending",
            fulfilled_at=datetime.utcnow() - timedelta(days=45),  # well past a 14-day window
        )
        db.session.add(order2)
        db.session.flush()
        db.session.add(OrderItem(
            order_id=order2.id, shopify_line_item_id="li-3", shopify_product_id="mock-prod-3",
            product_title="Kantha Embroidered Jacket", size="L", sku="JKT-KAN-L",
            quantity=1, price=3999.00, image_url=None,
        ))
        print("Created order 0987 (outside return window, COD, 1 item)")

    db.session.commit()
    print(f"\nDone. Log in at /api/auth/request-otp with email: {TEST_EMAIL}")
    print("(OTP will print to the console since RESEND_API_KEY isn't set)")
