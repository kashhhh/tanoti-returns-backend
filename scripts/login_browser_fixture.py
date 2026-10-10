"""Local-only login preview; no .env, production database or outbound requests.

customer@example.com / order 100: no phone.
whatsapp@example.com / order 200: registered phone.
Both use code 123456. Run with .test-venv/Scripts/python scripts/login_browser_fixture.py.
Point Vite TANOTI_DEV_API_TARGET at http://127.0.0.1:5010 (frontend port 5174).
"""
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_workflows import WorkflowTests, ADDRESS
from app.services import sync_service


if __name__ == "__main__":
    fixture = WorkflowTests()
    fixture.setUp()
    fixture.app.config.update(CORS_ORIGINS="http://127.0.0.1:5174,http://localhost:5174")
    customers = {
        "customer@example.com": {"id": "10", "email": "customer@example.com", "first_name": "Email", "last_name": "Tester"},
        "whatsapp@example.com": {"id": "20", "email": "whatsapp@example.com", "first_name": "WhatsApp", "last_name": "Tester", "phone": "+12025550123"},
    }
    orders = {}
    for number, customer in zip(("100", "200"), customers.values()):
        orders[number] = {"id": number, "order_number": number, "name": "#" + number,
            "email": customer["email"], "customer": customer, "shipping_address": ADDRESS,
            "financial_status": "paid", "gateway": "test",
            "line_items": [{"id": "1" if number == "100" else "2", "title": "Test Shirt", "quantity": 1, "price": "999"}],
            "fulfillments": [{"created_at": datetime.utcnow().isoformat(), "status": "success",
                              "line_items": [{"id": "1" if number == "100" else "2", "quantity": 1}]}]}
        sync_service.upsert_order(orders[number])
    with ExitStack() as mocks:
        mocks.enter_context(patch("requests.get", side_effect=AssertionError("No live network allowed")))
        mocks.enter_context(patch("app.auth.routes.shopify_client.find_customer_by_email", side_effect=customers.get))
        mocks.enter_context(patch("app.auth.routes.shopify_client.find_order_for_login", side_effect=orders.get))
        mocks.enter_context(patch("app.auth.routes._generate_otp", return_value="123456"))
        mocks.enter_context(patch("app.auth.routes.sync_service.refresh_item_images", return_value=None))
        fixture.ctx.pop()
        try:
            fixture.app.run(host="127.0.0.1", port=5010, debug=False, use_reloader=False, threaded=False)
        finally:
            fixture.ctx.push()
            fixture.tearDown()
