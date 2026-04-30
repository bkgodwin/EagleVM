from functools import wraps

from flask import abort, redirect, request, session, url_for
from flask_login import current_user


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.url))
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def remember_impersonation(admin_id, user_id):
    session["impersonated_by"] = admin_id
    session["impersonating_user"] = user_id


def clear_impersonation():
    session.pop("impersonated_by", None)
    session.pop("impersonating_user", None)


def is_impersonating():
    return "impersonated_by" in session
