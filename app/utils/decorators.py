from app.utils.sessions import session_required

login_required = session_required("customer")
