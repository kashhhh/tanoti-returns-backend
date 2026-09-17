"""
Minimal admin auth for Phase 1: a single shared admin key checked via
header, since this is a single-owner backend, not a multi-user admin
system. Swap for a proper login (Flask-Login / separate admin JWT) in
Phase 2 if you want multiple staff accounts with distinct permissions.
"""
from functools import wraps
from flask import request, jsonify, current_app


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-Admin-Key")
        if not key or key != current_app.config.get("ADMIN_API_KEY"):
            return jsonify({"error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper
