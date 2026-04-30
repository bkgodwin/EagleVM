from datetime import datetime, timezone

from flask_login import UserMixin

from . import bcrypt, db, login_manager


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    vm_id = db.Column(db.Integer, nullable=True, unique=True)
    override_cpu = db.Column(db.Integer, nullable=True)
    override_ram = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    vm_config = db.relationship("VMConfig", back_populates="user", uselist=False, cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)


class VMConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    vm_id = db.Column(db.Integer, unique=True, nullable=False)
    cpu = db.Column(db.Integer, nullable=False)
    ram = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(32), default="stopped", nullable=False)
    guac_connection_id = db.Column(db.String(128), nullable=True)
    last_ip = db.Column(db.String(64), nullable=True)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = db.relationship("User", back_populates="vm_config")


class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    default_cpu = db.Column(db.Integer, nullable=False, default=2)
    default_ram = db.Column(db.Integer, nullable=False, default=4096)
    proxmox_host = db.Column(db.String(255), nullable=True)
    proxmox_node = db.Column(db.String(128), nullable=True)
    template_vm_id = db.Column(db.Integer, nullable=False, default=9000)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def ensure_initial_data():
    from flask import current_app

    settings = db.session.get(Settings, 1)
    if settings is None:
        settings = Settings(
            id=1,
            default_cpu=current_app.config["DEFAULT_CPU"],
            default_ram=current_app.config["DEFAULT_RAM"],
            proxmox_host=current_app.config["PROXMOX_HOST"],
            proxmox_node=current_app.config["PROXMOX_NODE"],
            template_vm_id=current_app.config["PROXMOX_TEMPLATE_VM_ID"],
        )
        db.session.add(settings)

    admin = User.query.filter_by(email="admin@local").first()
    if admin is None:
        admin = User(email="admin@local", is_admin=True, must_change_password=True)
        admin.set_password("admin123")
        db.session.add(admin)

    db.session.commit()
