from flask import g, has_request_context
from app.extensions import db
from app.models import AdminAudit


def record_admin_action(action, target):
    """Commit alongside the mutation; no secrets, addresses or request bodies."""
    if has_request_context() and getattr(g, "admin_email", None):
        db.session.add(AdminAudit(actor=g.admin_email, action=action, target=target))
