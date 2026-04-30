import csv
import io

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user

from .. import db
from ..auth import admin_required, clear_impersonation, is_impersonating, remember_impersonation
from ..forms import CSVImportForm, ResourceOverrideForm, SettingsForm, UserCreateForm
from ..guac import GuacamoleClient, GuacamoleError
from ..models import Settings, User, VMConfig
from ..proxmox import ProxmoxClient, ProxmoxError

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/")
@login_required
@admin_required
def dashboard():
    users = User.query.order_by(User.email).all()
    settings = db.session.get(Settings, 1)
    return render_template(
        "admin/dashboard.html",
        users=users,
        settings=settings,
        csv_form=CSVImportForm(),
        settings_form=SettingsForm(obj=settings),
        user_form=UserCreateForm(),
    )


@admin_bp.route("/users", methods=["POST"])
@login_required
@admin_required
def create_user():
    form = UserCreateForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        if User.query.filter_by(email=email).first():
            flash("User already exists.", "warning")
        else:
            user = User(email=email, is_admin=form.is_admin.data)
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()
            flash("User created.", "success")
    else:
        flash("Could not create user. Check the form values.", "danger")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/import-users", methods=["POST"])
@login_required
@admin_required
def import_users():
    form = CSVImportForm()
    if not form.validate_on_submit():
        flash("Upload a CSV file with email,password columns.", "danger")
        return redirect(url_for("admin.dashboard"))
    stream = io.StringIO(form.csv_file.data.stream.read().decode("utf-8-sig"))
    reader = csv.DictReader(stream)
    if reader.fieldnames != ["email", "password"]:
        flash("CSV header must be exactly: email,password", "danger")
        return redirect(url_for("admin.dashboard"))
    created = skipped = 0
    for row in reader:
        email = (row.get("email") or "").lower().strip()
        password = row.get("password") or ""
        if not email or len(password) < 8 or User.query.filter_by(email=email).first():
            skipped += 1
            continue
        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        created += 1
    db.session.commit()
    flash(f"Imported {created} users. Skipped {skipped}.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/settings", methods=["POST"])
@login_required
@admin_required
def update_settings():
    settings = db.session.get(Settings, 1)
    form = SettingsForm()
    if form.validate_on_submit():
        form.populate_obj(settings)
        db.session.commit()
        flash("Settings updated.", "success")
    else:
        flash("Could not update settings.", "danger")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/users/<int:user_id>/resources", methods=["POST"])
@login_required
@admin_required
def update_resources(user_id):
    user = db.session.get(User, user_id)
    if user is None or user.is_admin:
        flash("Student user not found.", "danger")
        return redirect(url_for("admin.dashboard"))
    settings = db.session.get(Settings, 1)
    form = ResourceOverrideForm()
    if form.validate_on_submit():
        user.override_cpu = form.cpu.data
        user.override_ram = form.ram.data
        vm = user.vm_config
        if vm is not None:
            vm.cpu = form.cpu.data
            vm.ram = form.ram.data
            if vm.vm_id:
                try:
                    ProxmoxClient().configure_vm(vm.vm_id, vm.cpu, vm.ram)
                except ProxmoxError as exc:
                    flash(str(exc), "warning")
        flash("Resource override saved.", "success")
        db.session.commit()
    else:
        flash(f"Invalid resource values. Defaults are {settings.default_cpu} CPU and {settings.default_ram} MB.", "danger")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/users/<int:user_id>/reset-vm", methods=["POST"])
@login_required
@admin_required
def reset_vm(user_id):
    user = db.session.get(User, user_id)
    if user is None or user.vm_config is None:
        flash("User has no VM to reset.", "warning")
        return redirect(url_for("admin.dashboard"))
    try:
        ProxmoxClient().destroy_vm(user.vm_config.vm_id)
    except ProxmoxError as exc:
        flash(f"Destroy failed: {exc}", "danger")
        return redirect(url_for("admin.dashboard"))
    db.session.delete(user.vm_config)
    user.vm_id = None
    db.session.commit()
    flash("VM reset. It will be recreated on next launch.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/users/<int:user_id>/status", methods=["POST"])
@login_required
@admin_required
def refresh_user_status(user_id):
    user = db.session.get(User, user_id)
    if user and user.vm_config:
        try:
            user.vm_config.status = ProxmoxClient().status(user.vm_config.vm_id)
            db.session.commit()
        except ProxmoxError as exc:
            flash(str(exc), "danger")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/users/<int:user_id>/impersonate", methods=["POST"])
@login_required
@admin_required
def impersonate(user_id):
    user = db.session.get(User, user_id)
    if user is None or user.is_admin:
        flash("Cannot impersonate that account.", "danger")
        return redirect(url_for("admin.dashboard"))
    admin_id = current_user.id
    remember_impersonation(admin_id, user.id)
    login_user(user)
    flash(f"Impersonating {user.email}.", "info")
    return redirect(url_for("user.dashboard"))


@admin_bp.route("/stop-impersonating", methods=["POST"])
@login_required
def stop_impersonating():
    if not is_impersonating():
        return redirect(url_for("user.dashboard"))
    from flask import session

    admin = db.session.get(User, session["impersonated_by"])
    clear_impersonation()
    if admin:
        login_user(admin)
        flash("Returned to admin session.", "info")
        return redirect(url_for("admin.dashboard"))
    return redirect(url_for("auth.logout"))
