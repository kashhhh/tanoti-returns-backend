from datetime import datetime, timedelta
from functools import wraps

import jwt
from flask import current_app, request, jsonify, g

from app.models import Customer


def issue_token(customer: Customer) -> str:
    payload = {
        "customer_id": customer.id,
        "email": customer.email,
        "exp": datetime.utcnow() + timedelta(hours=current_app.config["JWT_EXPIRY_HOURS"]),
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")


def login_required(fn):
    """Reads a Bearer token, loads the Customer, sets g.customer.
    Also used by admin routes' own separate decorator (see admin/routes.py)
    -- this one is customer-facing only."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        token = auth_header.split(" ", 1)[1]
        try:
            payload = jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Session expired, please log in again"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid session"}), 401

        if payload.get("role") == "admin" or not payload.get("customer_id"):
            return jsonify({"error": "Invalid customer session"}), 401
        customer = Customer.query.get(payload["customer_id"])
        if not customer:
            return jsonify({"error": "Customer not found"}), 401

        g.customer = customer
        return fn(*args, **kwargs)
    return wrapper
