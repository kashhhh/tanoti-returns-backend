"""Loopback-only UI test fixture. No .env, real database or provider calls.

Run from backend with the test dependencies installed. Point Vite's optional
TANOTI_DEV_API_TARGET at http://127.0.0.1:5009. This script is never a deployment
entry point. Customer: customer@example.com / 123456.
Admin: staff@example.com / test-admin-secret / 111111.
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_workflows import WorkflowTests


if __name__ == "__main__":
    fixture = WorkflowTests()
    fixture.setUp()
    fixture.app.config.update(CORS_ORIGINS="http://127.0.0.1:5173,http://localhost:5173", RATE_LIMIT_ENABLED=False)
    with patch("requests.get", side_effect=AssertionError("No live network allowed")), \
            patch("app.auth.routes.shopify_client.find_customer_by_email", return_value={"id": "100"}), \
            patch("app.auth.routes._generate_otp", return_value="123456"), \
            patch("app.auth.admin_routes.secrets.choice", return_value="1"):
        assert fixture.submit().status_code == 201
        if "--operations" in sys.argv:
            from datetime import datetime, timedelta
            from app.extensions import db
            from app.models import ReturnRequest, RequestStatus, ShippingBooking
            req = ReturnRequest.query.one()
            req.status = RequestStatus.PICKUP_SCHEDULED
            req.photo_decision_at = datetime.utcnow() - timedelta(days=4)
            req.pickup_carrier, req.pickup_tracking_id, req.pickup_status = "delhivery", "1234567890123", "Scheduled"
            db.session.add(ShippingBooking(reference=req.return_number + "-R", request_number=req.return_number,
                leg="pickup", environment="staging", warehouse="Test warehouse", state="confirmed",
                waybill=req.pickup_tracking_id, carrier_status="Scheduled"))
            db.session.commit()
        fixture.ctx.pop()
        try:
            fixture.app.run(host="127.0.0.1", port=5009, debug=False, use_reloader=False, threaded=False)
        finally:
            fixture.ctx.push()
            fixture.tearDown()
