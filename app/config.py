import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-this-secret-key")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'vmclassroom.sqlite3'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    WTF_CSRF_TIME_LIMIT = None
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"

    PROXMOX_HOST = os.environ.get("PROXMOX_HOST", "")
    PROXMOX_NODE = os.environ.get("PROXMOX_NODE", "")
    PROXMOX_VERIFY_SSL = os.environ.get("PROXMOX_VERIFY_SSL", "true").lower() == "true"
    PROXMOX_API_TOKEN_ID = os.environ.get("PROXMOX_API_TOKEN_ID", "")
    PROXMOX_API_TOKEN_SECRET = os.environ.get("PROXMOX_API_TOKEN_SECRET", "")
    PROXMOX_TEMPLATE_VM_ID = int(os.environ.get("PROXMOX_TEMPLATE_VM_ID", "9000"))
    PROXMOX_VM_ID_START = int(os.environ.get("PROXMOX_VM_ID_START", "2000"))
    PROXMOX_STORAGE = os.environ.get("PROXMOX_STORAGE", "")
    PROXMOX_FULL_CLONE = os.environ.get("PROXMOX_FULL_CLONE", "true").lower() == "true"
    PROXMOX_VM_BRIDGE = os.environ.get("PROXMOX_VM_BRIDGE", "")

    GUAC_BASE_URL = os.environ.get("GUAC_BASE_URL", "http://127.0.0.1:8080/guacamole")
    GUAC_PUBLIC_URL = os.environ.get("GUAC_PUBLIC_URL", "/guacamole")
    GUAC_ADMIN_USER = os.environ.get("GUAC_ADMIN_USER", "guacadmin")
    GUAC_ADMIN_PASSWORD = os.environ.get("GUAC_ADMIN_PASSWORD", "")
    GUAC_DATASOURCE = os.environ.get("GUAC_DATASOURCE", "mysql")
    GUAC_CONNECTION_GROUP = os.environ.get("GUAC_CONNECTION_GROUP", "ROOT")
    GUAC_RDP_USERNAME = os.environ.get("GUAC_RDP_USERNAME", "")
    GUAC_RDP_PASSWORD = os.environ.get("GUAC_RDP_PASSWORD", "")
    GUAC_RDP_DOMAIN = os.environ.get("GUAC_RDP_DOMAIN", "")
    GUAC_IGNORE_CERT = os.environ.get("GUAC_IGNORE_CERT", "true").lower() == "true"
    GUAC_SECURITY_MODE = os.environ.get("GUAC_SECURITY_MODE", "any")
    GUAC_ENABLE_DRIVE = os.environ.get("GUAC_ENABLE_DRIVE", "false").lower() == "true"

    DEFAULT_CPU = int(os.environ.get("DEFAULT_CPU", "2"))
    DEFAULT_RAM = int(os.environ.get("DEFAULT_RAM", "4096"))
