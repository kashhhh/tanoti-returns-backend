"""
Resend wrapper. Install with: pip install resend
"""
import resend
from flask import current_app
from app.services.email_templates import login_email


def _configure():
    resend.api_key = current_app.config["RESEND_API_KEY"]


def _local_mode() -> bool:
    """Explicit console-only email delivery for both login flows."""
    return current_app.config.get("TESTING_MODE", False)


def send_otp_email(to_email: str, otp_code: str):
    if _local_mode():
        print(f"[LOCAL EMAIL] OTP for {to_email}: {otp_code}", flush=True)
        return "console"
    if not current_app.config.get("RESEND_API_KEY"):
        raise RuntimeError("Configure Resend to send login codes")
    _configure()
    resend.Emails.send({
        "from": current_app.config["EMAIL_FROM"],
        "to": [to_email],
        "subject": "Your Tanoti returns login code",
        "html": login_email(otp_code, current_app.config['OTP_EXPIRY_MINUTES']),
    })


def send_rejection_email(to_email: str, item_title: str, request_number: str, rejection_reason: str, note: str = ""):
    if _local_mode():
        print(f"[LOCAL EMAIL] Rejection for {to_email} ({request_number}): {rejection_reason} -- {note}")
        return
    _configure()
    body = (
        f"<p>Your return/exchange request <strong>{request_number}</strong> "
        f"for <strong>{item_title}</strong> could not be accepted.</p>"
        f"<p>Reason: {rejection_reason}</p>"
    )
    if note:
        body += f"<p>{note}</p>"
    resend.Emails.send({
        "from": current_app.config["EMAIL_FROM"],
        "to": [to_email],
        "subject": f"Update on your return request {request_number}",
        "html": body,
    })


def send_status_update_email(to_email: str, request_number: str, status: str):
    if _local_mode():
        print(f"[LOCAL EMAIL] {to_email}: {request_number} is now {status}")
        return
    _configure()
    resend.Emails.send({
        "from": current_app.config["EMAIL_FROM"],
        "to": [to_email],
        "subject": f"Your request {request_number} is now {status}",
        "html": f"<p>Your request <strong>{request_number}</strong> status: <strong>{status}</strong>.</p>",
    })


def send_admin_otp_email(to_email, code):
    if _local_mode():
        print(f"[LOCAL ADMIN EMAIL] OTP for {to_email}: {code}", flush=True)
        return "console"
    if not current_app.config.get("RESEND_API_KEY"):
        raise RuntimeError("Configure Resend to send admin login codes")
    _configure()
    resend.Emails.send({"from": current_app.config["EMAIL_FROM"], "to": [to_email],
                        "subject": "Your Tanoti admin login code",
                        "html": login_email(code, 10, admin=True)})
    return "email"
