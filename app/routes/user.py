from flask import Blueprint, flash, redirect, render_template, url_for
from flask_login import current_user, login_required

from .. import db
from ..guac import GuacamoleClient, GuacamoleError, guac_password_for, guac_username_for
from ..models import Settings, VMConfig
from ..proxmox import ProxmoxClient, ProxmoxError, VMIdentity, allocate_vm_id

user_bp = Blueprint("user", __name__)


@user_bp.route("/")
def index():
    return redirect(url_for("user.dashboard"))


@user_bp.route("/dashboard")
@login_required
def dashboard():
    vm = current_user.vm_config
    return render_template("dashboard.html", vm=vm)


@user_bp.route("/launch", methods=["POST"])
@login_required
def launch_vm():
    if current_user.is_admin:
        flash("Admins should use impersonation to launch a student's VM.", "warning")
        return redirect(url_for("admin.dashboard"))
    try:
        settings = db.session.get(Settings, 1)
        proxmox = ProxmoxClient()
        vm = current_user.vm_config
        if vm is None:
            vm_id = allocate_vm_id(current_user.id)
            vm = VMConfig(
                user_id=current_user.id,
                vm_id=vm_id,
                cpu=current_user.override_cpu or settings.default_cpu,
                ram=current_user.override_ram or settings.default_ram,
                status="creating",
            )
            current_user.vm_id = vm_id
            db.session.add(vm)
            db.session.commit()
            proxmox.clone_vm(
                settings.template_vm_id,
                VMIdentity(vm_id=vm_id, name=f"vm-{current_user.id}"),
                cpu=vm.cpu,
                ram=vm.ram,
            )
        vm.status = "starting"
        db.session.commit()
        proxmox.start_vm(vm.vm_id)
        ip_address = proxmox.guest_ip(vm.vm_id)
        if not ip_address:
            ip_address = f"vm-{current_user.id}"
        guac = GuacamoleClient()
        guac_user = guac_username_for(current_user)
        guac_pass = guac_password_for(current_user)
        guac.ensure_user(guac_user, guac_pass)
        connection_id = guac.create_or_update_connection(
            name=f"VM {current_user.id}",
            hostname=ip_address,
            username=None,
            password=None,
            connection_id=vm.guac_connection_id,
        )
        guac.assign_connection_to_user(guac_user, connection_id)
        vm.guac_connection_id = connection_id
        vm.last_ip = ip_address
        vm.status = "running"
        db.session.commit()
        return redirect(url_for("user.viewer"))
    except (ProxmoxError, GuacamoleError) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("user.dashboard"))


@user_bp.route("/viewer")
@login_required
def viewer():
    vm = current_user.vm_config
    if vm is None or not vm.guac_connection_id:
        flash("Launch your VM before opening the viewer.", "warning")
        return redirect(url_for("user.dashboard"))
    try:
        guac = GuacamoleClient()
        url = guac.connection_url_for_user(
            vm.guac_connection_id,
            guac_username_for(current_user),
            guac_password_for(current_user),
        )
        return render_template("viewer.html", vm=vm, guac_url=url)
    except GuacamoleError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("user.dashboard"))


@user_bp.route("/refresh-status", methods=["POST"])
@login_required
def refresh_status():
    vm = current_user.vm_config
    if not vm:
        return redirect(url_for("user.dashboard"))
    try:
        vm.status = ProxmoxClient().status(vm.vm_id)
        db.session.commit()
    except ProxmoxError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("user.dashboard"))
