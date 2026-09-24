from app.utils.sessions import session_required

admin_required = session_required("admin")
