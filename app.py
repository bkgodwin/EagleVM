import asyncio
import base64
import csv
import fcntl
import getpass
import json
import logging
import os
import re
import shlex
import socket
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import paramiko
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
import uvicorn


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("LXCHOSTER_CONFIG", BASE_DIR / "config.json"))
STATE_DIR = Path(os.environ.get("LXCHOSTER_STATE_DIR", "/var/lib/lxchoster"))
STATE_PATH = STATE_DIR / "sessions.json"
SESSION_HISTORY_PATH = STATE_DIR / "session_history.csv"
LOCK_PATH = STATE_DIR / "sessions.lock"
SECRET_KEY_PATH = STATE_DIR / "secret.key"
PROXMOX_PASSWORD_KEY = "proxmox_root_password_encrypted"
GUI_VM_PASSWORD_KEY = "gui_vm_password_encrypted"
CLIENT_COOKIE_NAME = "lxchoster_client_id"
SESSION_HISTORY_FIELDS = ["client_id", "session_id", "vmid", "hostname", "status", "updated_at"]
COOKIE_MAX_AGE_SECONDS = 31536000
MAX_CLIENT_ID_LENGTH = 120
# Validation pattern for the GUI OS username: must start with a letter, followed by
# up to 31 alphanumeric characters or the symbols . _ @ - (max 32 chars total).
GUI_VM_USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.@-]{0,31}$")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_state()
    await asyncio.to_thread(ensure_proxmox_password)
    cfg = load_config()
    if cfg.get("is_gui", False):
        await asyncio.to_thread(ensure_gui_vm_password)
    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)


app = FastAPI(title="LXChoster", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lxchoster")
_PROXMOX_PASSWORD_CACHE: str | None = None
_GUI_VM_PASSWORD_CACHE: str | None = None
_FERNET_CACHE: Fernet | None = None


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open() as fh:
        cfg = json.load(fh)
    cfg.setdefault("ssh_user", "root")
    cfg.setdefault("ssh_known_hosts_path", str(BASE_DIR / "known_hosts"))
    cfg.setdefault(PROXMOX_PASSWORD_KEY, "")
    cfg.setdefault("storage", "local-lvm")
    cfg.setdefault("bridge", "vmbr0")
    cfg.setdefault("max_sessions", 25)
    cfg.setdefault("session_ttl_seconds", 600)
    cfg.setdefault("boot_timeout_seconds", 90)
    cfg.setdefault("is_gui", False)
    cfg.setdefault("gui_vm_username", "")
    cfg.setdefault(GUI_VM_PASSWORD_KEY, "")
    cfg.setdefault(
        "local_network_blocklist",
        [
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "169.254.0.0/16",
            "100.64.0.0/10",
            "fc00::/7",
            "fe80::/10",
        ],
    )
    cfg.setdefault("local_network_allowlist", [])
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    payload = json.dumps(cfg, indent=2, sort_keys=True) + "\n"
    tmp = CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(payload)
    tmp.replace(CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def init_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        STATE_PATH.write_text("{}")
    if not SESSION_HISTORY_PATH.exists():
        SESSION_HISTORY_PATH.write_text(",".join(SESSION_HISTORY_FIELDS) + "\n")


class StateLock:
    def __enter__(self):
        init_state()
        self.fh = LOCK_PATH.open("w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def read_state() -> dict[str, Any]:
    init_state()
    try:
        return json.loads(STATE_PATH.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def write_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def read_session_history() -> list[dict[str, str]]:
    init_state()
    rows: list[dict[str, str]] = []
    with SESSION_HISTORY_PATH.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            client_id = row.get("client_id", "").strip()
            session_id = row.get("session_id", "").strip()
            if client_id and session_id:
                rows.append(
                    {
                        "client_id": client_id,
                        "session_id": session_id,
                        "vmid": row.get("vmid", "").strip(),
                        "hostname": row.get("hostname", "").strip(),
                        "status": row.get("status", "").strip(),
                        "updated_at": row.get("updated_at", "").strip(),
                    }
                )
    return rows


def write_session_history(rows: list[dict[str, str]]) -> None:
    tmp = SESSION_HISTORY_PATH.with_suffix(".tmp")
    with tmp.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SESSION_HISTORY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(SESSION_HISTORY_PATH)


def upsert_session_history(client_id: str, session_id: str, session: dict[str, Any], now: int) -> None:
    rows = [
        row
        for row in read_session_history()
        if row.get("client_id") != client_id and row.get("session_id") != session_id
    ]
    rows.append(
        {
            "client_id": client_id,
            "session_id": session_id,
            "vmid": str(session.get("vmid", "")),
            "hostname": str(session.get("hostname", "")),
            "status": str(session.get("status", "")),
            "updated_at": str(now),
        }
    )
    write_session_history(rows)


def remove_session_history(session_id: str) -> None:
    rows = [row for row in read_session_history() if row.get("session_id") != session_id]
    write_session_history(rows)


def attach_client_cookie(response: JSONResponse, request: Request, client_id: str) -> None:
    response.set_cookie(
        key=CLIENT_COOKIE_NAME,
        value=client_id,
        max_age=COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


def normalize_client_id(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if len(cleaned) > MAX_CLIENT_ID_LENGTH:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cleaned):
        return None
    return cleaned


async def apply_network_restrictions(vmid: int, cfg: dict[str, Any]) -> None:
    blocklist = [str(item).strip() for item in cfg.get("local_network_blocklist", []) if str(item).strip()]
    allowlist = [str(item).strip() for item in cfg.get("local_network_allowlist", []) if str(item).strip()]
    allow_set = set(allowlist)
    blocked = [cidr for cidr in blocklist if cidr not in allow_set]
    if not blocked:
        return

    v4_allow = [cidr for cidr in allowlist if ":" not in cidr]
    v6_allow = [cidr for cidr in allowlist if ":" in cidr]
    v4_block = [cidr for cidr in blocked if ":" not in cidr]
    v6_block = [cidr for cidr in blocked if ":" in cidr]

    lines = ["set -eu"]
    if v4_allow or v4_block:
        lines.append(
            "command -v iptables >/dev/null 2>&1 || { "
            "echo 'iptables not found - network restrictions cannot be applied' >&2; exit 1; }"
        )
        for cidr in v4_allow:
            q = shlex.quote(cidr)
            lines.append(
                f"iptables -C OUTPUT -d {q} -j ACCEPT >/dev/null 2>&1 || iptables -I OUTPUT 1 -d {q} -j ACCEPT"
            )
        for cidr in v4_block:
            q = shlex.quote(cidr)
            lines.append(
                f"iptables -C OUTPUT -d {q} -j REJECT >/dev/null 2>&1 || iptables -A OUTPUT -d {q} -j REJECT"
            )
    if v6_allow or v6_block:
        lines.append(
            "command -v ip6tables >/dev/null 2>&1 || { "
            "echo 'ip6tables not found - network restrictions cannot be applied' >&2; exit 1; }"
        )
        for cidr in v6_allow:
            q = shlex.quote(cidr)
            lines.append(
                f"ip6tables -C OUTPUT -d {q} -j ACCEPT >/dev/null 2>&1 || ip6tables -I OUTPUT 1 -d {q} -j ACCEPT"
            )
        for cidr in v6_block:
            q = shlex.quote(cidr)
            lines.append(
                f"ip6tables -C OUTPUT -d {q} -j REJECT >/dev/null 2>&1 || ip6tables -A OUTPUT -d {q} -j REJECT"
            )

    script = "\n".join(lines)
    await run_ssh(f"pct exec {vmid} -- sh -lc {shlex.quote(script)}", timeout=60)


def get_fernet() -> Fernet:
    global _FERNET_CACHE
    if _FERNET_CACHE is not None:
        return _FERNET_CACHE
    init_state()
    if SECRET_KEY_PATH.exists():
        key = SECRET_KEY_PATH.read_bytes()
    else:
        key = Fernet.generate_key()
        tmp = SECRET_KEY_PATH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        tmp.replace(SECRET_KEY_PATH)
    os.chmod(SECRET_KEY_PATH, 0o600)
    _FERNET_CACHE = Fernet(key)
    return _FERNET_CACHE


def encrypt_secret(value: str) -> str:
    return get_fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str, secret_label: str, config_key: str) -> str:
    try:
        return get_fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            f"Failed to decrypt stored {secret_label}. "
            f"If needed, clear {config_key} in {CONFIG_PATH.name} and restart to re-enter it."
        ) from exc


def prompt_for_proxmox_password() -> str:
    if not os.isatty(0):
        raise RuntimeError(
            "No encrypted Proxmox root password is configured and no interactive terminal is available. "
            "Start once in an interactive terminal to save it, or set proxmox_root_password_encrypted in config.json."
        )
    password = getpass.getpass("Enter Proxmox root password: ")
    if not password:
        raise RuntimeError("Proxmox root password cannot be empty.")
    return password


def prompt_for_gui_vm_password() -> str:
    return getpass.getpass("Enter GUI VM user password (displayed to session users): ")


def ensure_proxmox_password() -> str:
    global _PROXMOX_PASSWORD_CACHE
    if _PROXMOX_PASSWORD_CACHE is not None:
        return _PROXMOX_PASSWORD_CACHE

    cfg = load_config()
    encrypted = cfg.get(PROXMOX_PASSWORD_KEY, "").strip()
    if encrypted:
        try:
            password = decrypt_secret(encrypted, "Proxmox root password", PROXMOX_PASSWORD_KEY)
        except RuntimeError as exc:
            if not os.isatty(0):
                raise RuntimeError(
                    f"{exc} Start once in an interactive terminal after clearing that config key to save a replacement."
                ) from exc
            logger.warning(
                "Stored Proxmox root password could not be decrypted; prompting for a replacement."
            )
            password = prompt_for_proxmox_password()
            cfg[PROXMOX_PASSWORD_KEY] = encrypt_secret(password)
            save_config(cfg)
            logger.info("Encrypted Proxmox root password stored in %s", CONFIG_PATH)
    else:
        password = prompt_for_proxmox_password()
        cfg[PROXMOX_PASSWORD_KEY] = encrypt_secret(password)
        save_config(cfg)
        logger.info("Encrypted Proxmox root password stored in %s", CONFIG_PATH)
    _PROXMOX_PASSWORD_CACHE = password
    return password


def ensure_gui_vm_password() -> str:
    """Load, decrypt, or prompt for the GUI VM user password.

    The password is stored encrypted in config.json under ``gui_vm_password_encrypted``.
    If the field is blank and an interactive terminal is available the admin is prompted once
    and the encrypted value is persisted.  Returns an empty string when ``is_gui`` is enabled
    but no password has been configured and no terminal is available (non-fatal).
    """
    global _GUI_VM_PASSWORD_CACHE
    if _GUI_VM_PASSWORD_CACHE is not None:
        return _GUI_VM_PASSWORD_CACHE

    cfg = load_config()
    encrypted = cfg.get(GUI_VM_PASSWORD_KEY, "").strip()
    password = ""
    if encrypted:
        try:
            password = decrypt_secret(encrypted, "GUI VM password", GUI_VM_PASSWORD_KEY)
        except RuntimeError as exc:
            logger.debug("Stored GUI VM password decryption failed", exc_info=exc)
            if os.isatty(0):
                logger.warning("Stored GUI VM password could not be decrypted; prompting for a replacement.")
                prompted_password = prompt_for_gui_vm_password()
                if prompted_password:
                    password = prompted_password
                    cfg[GUI_VM_PASSWORD_KEY] = encrypt_secret(password)
                    save_config(cfg)
                    logger.info("Encrypted GUI VM password stored in %s", CONFIG_PATH)
            else:
                logger.warning(
                    "Stored GUI VM password could not be decrypted. Clear the GUI VM password setting in %s "
                    "to re-enter it later. No credentials will be shown.",
                    CONFIG_PATH,
                )
    elif os.isatty(0):
        prompted_password = prompt_for_gui_vm_password()
        if prompted_password:
            password = prompted_password
            cfg[GUI_VM_PASSWORD_KEY] = encrypt_secret(password)
            save_config(cfg)
            logger.info("Encrypted GUI VM password stored in %s", CONFIG_PATH)
    else:
        logger.warning(
            "is_gui is enabled but gui_vm_password_encrypted is not set. "
            "No credentials will be shown. Set it in %s.",
            CONFIG_PATH,
        )
        password = ""
    _GUI_VM_PASSWORD_CACHE = password
    return password


def active_session_count(state: dict[str, Any]) -> int:
    """Return the number of currently active sessions (creating/running)."""
    return sum(1 for session in state.values() if session.get("status") in {"creating", "running"})


def open_ssh_client(timeout: int = 10) -> paramiko.SSHClient:
    cfg = load_config()
    password = ensure_proxmox_password()
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    known_hosts_value = cfg.get("ssh_known_hosts_path", "")
    known_hosts_display = "system known_hosts"
    if known_hosts_value:
        known_hosts = Path(known_hosts_value)
        known_hosts_display = str(known_hosts)
        if known_hosts.exists():
            client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            hostname=cfg["proxmox_host"],
            username=cfg["ssh_user"],
            password=password,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
    except paramiko.BadHostKeyException as exc:
        client.close()
        raise RuntimeError(
            f"SSH host key verification failed for {cfg['proxmox_host']}. "
            f"Add the host key to {known_hosts_display} or system known_hosts "
            f"(for example: ssh-keyscan -H {cfg['proxmox_host']} >> {known_hosts_display})."
        ) from exc
    except paramiko.AuthenticationException as exc:
        client.close()
        raise RuntimeError(f"SSH authentication failed for {cfg['ssh_user']}@{cfg['proxmox_host']}.") from exc
    except (paramiko.SSHException, OSError) as exc:
        client.close()
        raise RuntimeError(f"SSH connection to {cfg['proxmox_host']} failed: {exc}") from exc
    return client


def ssh_cmd(remote: str, timeout: int = 60) -> str:
    client = open_ssh_client(timeout=timeout)
    try:
        _, stdout, stderr = client.exec_command(remote, timeout=timeout)
        stdout.channel.settimeout(timeout)
        stderr.channel.settimeout(timeout)
        try:
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode(errors="replace").strip()
            err = stderr.read().decode(errors="replace").strip()
        except socket.timeout as exc:
            raise RuntimeError(f"Remote command timed out after {timeout} seconds.") from exc
    finally:
        client.close()
    if exit_code:
        raise RuntimeError(err or out or f"remote command failed with exit code {exit_code}")
    return out


def validate_session_id(session_id: str) -> None:
    if not session_id or any(c not in "0123456789abcdef" for c in session_id):
        raise ValueError("invalid session id")


async def run_ssh(remote: str, timeout: int = 60) -> str:
    return await asyncio.to_thread(ssh_cmd, remote, timeout)


async def allocate_vmid() -> int:
    return int(await run_ssh("pvesh get /cluster/nextid"))


async def cleanup_session(session_id: str, reason: str = "cleanup") -> None:
    validate_session_id(session_id)
    with StateLock():
        state = read_state()
        session = state.get(session_id)
        if not session or session.get("status") == "cleaned":
            return
        session["status"] = "cleaning"
        session["cleanup_reason"] = reason
        session["updated_at"] = int(time.time())
        state[session_id] = session
        write_state(state)

    vmid = int(session["vmid"])
    for command in (
        f"pct shutdown {vmid} --timeout 10 || true",
        f"pct stop {vmid} || true",
        f"pct destroy {vmid} --purge 1 --destroy-unreferenced-disks 1",
    ):
        await run_ssh(command, timeout=120)

    with StateLock():
        state = read_state()
        state[session_id] = {
            **state.get(session_id, session),
            "status": "cleaned",
            "cleaned_at": int(time.time()),
            "cleanup_reason": reason,
        }
        write_state(state)
        remove_session_history(session_id)
        logger.info(
            "Session deleted id=%s vmid=%s reason=%s active_sessions=%d",
            session_id,
            vmid,
            reason,
            active_session_count(state),
        )


async def wait_for_container(vmid: int, timeout: int) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            output = await run_ssh(f"pct exec {vmid} -- hostname -I", timeout=15)
            if output:
                return output.split()[0].strip()
        except Exception:
            pass
        await asyncio.sleep(2)
    raise TimeoutError("container did not boot with an IPv4 address")


async def start_gui_services(vmid: int, vnc_password: str) -> None:
    """Start Xvfb, a desktop environment, and x11vnc inside the container.

    The container template must have ``xvfb``, ``x11vnc``, and at least one of
    ``xfce4``, ``openbox``, or ``fluxbox`` installed.  All processes are started
    in the background with ``nohup`` so they outlive the ``pct exec`` shell.

    A per-session VNC password is written to ``/tmp/.vncpw`` inside the container
    and passed to x11vnc via ``-rfbauth`` for defense-in-depth.  The password
    contains only hex characters (0-9, a-f) and is safe to embed unquoted.
    """
    # vnc_password is secrets.token_hex(4): hex chars only, safe to embed without quoting.
    script = (
        "set -e; "
        "export DISPLAY=:99; "
        "nohup Xvfb :99 -screen 0 1280x800x24 -ac >/tmp/xvfb.log 2>&1 & "
        "sleep 2; "
        "if command -v startxfce4 >/dev/null 2>&1; then "
        "  nohup startxfce4 >/tmp/desktop.log 2>&1 & "
        "elif command -v openbox-session >/dev/null 2>&1; then "
        "  nohup openbox-session >/tmp/desktop.log 2>&1 & "
        "elif command -v fluxbox >/dev/null 2>&1; then "
        "  nohup fluxbox >/tmp/desktop.log 2>&1 & "
        "fi; "
        "sleep 1; "
        f"x11vnc -storepasswd {vnc_password} /tmp/.vncpw; "
        "nohup x11vnc -display :99 -rfbauth /tmp/.vncpw -forever -rfbport 5900 >/tmp/x11vnc.log 2>&1 &"
    )
    await run_ssh(f"pct exec {vmid} -- bash -c {shlex.quote(script)}", timeout=30)


async def wait_for_vnc(vmid: int, timeout: int) -> None:
    """Poll until x11vnc is listening on port 5900 inside the container."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = await run_ssh(
                f"pct exec {vmid} -- bash -c "
                "'ss -tlnp 2>/dev/null | grep -q :5900 && echo ok || true'",
                timeout=10,
            )
            if out.strip() == "ok":
                return
        except Exception:
            pass
        await asyncio.sleep(2)
    raise TimeoutError("VNC server (x11vnc) did not start within timeout")


def clamp_terminal_size(rows: int, cols: int) -> tuple[int, int]:
    rows = max(10, min(int(rows), 200))
    cols = max(20, min(int(cols), 400))
    return rows, cols


@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE_DIR / "static" / "index.html").read_text()


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.post("/api/session")
async def launch(request: Request):
    cfg = load_config()
    is_gui = bool(cfg.get("is_gui", False))
    gui_vm_username = str(cfg.get("gui_vm_username", "")).strip() if is_gui else ""
    gui_vm_password = ensure_gui_vm_password() if is_gui else ""
    password = secrets.token_urlsafe(24)
    # Per-session VNC password (8 hex chars) used for defense-in-depth VNC auth.
    vnc_password = secrets.token_hex(4) if is_gui else ""
    now = int(time.time())
    session_ttl = int(cfg["session_ttl_seconds"])
    presented_client_id = normalize_client_id(request.cookies.get(CLIENT_COOKIE_NAME))
    issued_client_id = secrets.token_urlsafe(18)
    with StateLock():
        state = read_state()
        history_rows = read_session_history()
        history_by_client = {row["client_id"]: row for row in history_rows}
        remembered = history_by_client.get(presented_client_id) if presented_client_id else None
        if remembered:
            remembered_session_id = remembered.get("session_id", "")
            remembered_session = state.get(remembered_session_id)
            if (
                remembered_session
                and remembered_session.get("status") == "running"
                and remembered_session.get("client_id") == presented_client_id
                and int(remembered_session.get("last_seen", 0)) >= (now - session_ttl)
            ):
                remembered_session["client_id"] = issued_client_id
                remembered_session["last_seen"] = now
                state[remembered_session_id] = remembered_session
                write_state(state)
                upsert_session_history(issued_client_id, remembered_session_id, remembered_session, now)
                payload: dict[str, Any] = {
                    "session_id": remembered_session_id,
                    "vmid": remembered_session["vmid"],
                    "hostname": remembered_session["hostname"],
                    "ip": remembered_session["ip"],
                    "reused": True,
                    "is_gui": is_gui,
                }
                if is_gui:
                    payload["gui_vm_username"] = gui_vm_username
                    payload["gui_vm_password"] = gui_vm_password
                    payload["gui_vnc_password"] = str(remembered_session.get("vnc_password", ""))
                response = JSONResponse(payload)
                attach_client_cookie(response, request, issued_client_id)
                return response
            history_rows = [
                row for row in history_rows if row.get("client_id") != presented_client_id
            ]
            write_session_history(history_rows)

        active = [s for s in state.values() if s.get("status") in {"creating", "running"}]
        if len(active) >= int(cfg["max_sessions"]):
            raise HTTPException(status_code=429, detail="Maximum active sessions reached.")

    session_id = secrets.token_hex(3)
    vmid = await allocate_vmid()
    hostname = f"temp-{session_id}"

    with StateLock():
        state = read_state()
        state[session_id] = {
            "vmid": vmid,
            "hostname": hostname,
            "client_id": issued_client_id,
            "status": "creating",
            "created_at": now,
            "last_seen": now,
            "is_gui": is_gui,
            "vnc_password": vnc_password,
        }
        write_state(state)
        upsert_session_history(issued_client_id, session_id, state[session_id], now)

    try:
        await run_ssh(
            f"pct clone {int(cfg['template_id'])} {vmid} --hostname {hostname} "
            f"--full 1 --storage {cfg['storage']}",
            timeout=300,
        )
        await run_ssh(
            f"pct set {vmid} --net0 name=eth0,bridge={cfg['bridge']},ip=dhcp --features nesting=1",
            timeout=60,
        )
        await run_ssh(f"pct start {vmid}", timeout=120)
        password_b64 = base64.b64encode(password.encode()).decode()
        await run_ssh(
            f"pct exec {vmid} -- sh -lc '{{ printf root:; printf {password_b64} | base64 -d; printf \"\\n\"; }} | chpasswd'",
            timeout=60,
        )
        if is_gui and gui_vm_username and gui_vm_password:
            if GUI_VM_USERNAME_PATTERN.fullmatch(gui_vm_username):
                gui_pw_b64 = base64.b64encode(gui_vm_password.encode()).decode()
                await run_ssh(
                    f"pct exec {vmid} -- sh -lc "
                    f"'{{ printf {shlex.quote(gui_vm_username)}:; printf {gui_pw_b64} | base64 -d; "
                    f"printf \"\\n\"; }} | chpasswd'",
                    timeout=60,
                )
            else:
                logger.warning(
                    "gui_vm_username %r contains invalid characters; skipping chpasswd",
                    gui_vm_username,
                )
        await apply_network_restrictions(vmid, cfg)
        ip = await wait_for_container(vmid, int(cfg["boot_timeout_seconds"]))
        if is_gui:
            await start_gui_services(vmid, vnc_password)
            await wait_for_vnc(vmid, int(cfg["boot_timeout_seconds"]))
    except Exception as exc:
        await cleanup_session(session_id, f"create_failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    with StateLock():
        state = read_state()
        state[session_id].update({"status": "running", "ip": ip, "last_seen": int(time.time())})
        write_state(state)
        upsert_session_history(issued_client_id, session_id, state[session_id], int(time.time()))
        logger.info(
            "Session created id=%s vmid=%s hostname=%s is_gui=%s active_sessions=%d",
            session_id,
            vmid,
            hostname,
            is_gui,
            active_session_count(state),
        )
    payload = {"session_id": session_id, "vmid": vmid, "hostname": hostname, "ip": ip, "reused": False, "is_gui": is_gui}
    if is_gui:
        payload["gui_vm_username"] = gui_vm_username
        payload["gui_vm_password"] = gui_vm_password
        payload["gui_vnc_password"] = vnc_password
    response = JSONResponse(payload)
    attach_client_cookie(response, request, issued_client_id)
    return response


@app.websocket("/ws/{session_id}")
async def terminal(websocket: WebSocket, session_id: str):
    validate_session_id(session_id)
    await websocket.accept()

    with StateLock():
        state = read_state()
        session = state.get(session_id)
        if not session or session.get("status") != "running":
            await websocket.close(code=1008)
            return
        session["last_seen"] = int(time.time())
        state[session_id] = session
        write_state(state)
        if session.get("client_id"):
            upsert_session_history(session["client_id"], session_id, session, int(time.time()))

    vmid = int(session["vmid"])
    client_id = str(session.get("client_id", ""))
    client = await asyncio.to_thread(open_ssh_client, 10)
    transport = client.get_transport()
    if transport is None:
        logger.error("Failed to open SSH transport for session_id=%s vmid=%s", session_id, vmid)
        client.close()
        await websocket.close(code=1011)
        return
    channel = await asyncio.to_thread(transport.open_session, timeout=10)
    await asyncio.to_thread(channel.get_pty, term="xterm", width=80, height=24)
    await asyncio.to_thread(channel.exec_command, f"pct exec {vmid} -- bash -l")

    async def pty_to_ws():
        while True:
            if await asyncio.to_thread(channel.recv_ready):
                data = await asyncio.to_thread(channel.recv, 4096)
                if not data:
                    break
                if (
                    websocket.application_state != WebSocketState.CONNECTED
                    or websocket.client_state != WebSocketState.CONNECTED
                ):
                    break
                try:
                    await websocket.send_bytes(data)
                except RuntimeError:
                    if (
                        websocket.application_state != WebSocketState.CONNECTED
                        or websocket.client_state != WebSocketState.CONNECTED
                    ):
                        break
                    raise
                continue
            if await asyncio.to_thread(channel.exit_status_ready):
                break
            await asyncio.sleep(0.05)

    async def ws_to_pty():
        while True:
            try:
                msg = await websocket.receive()
            except WebSocketDisconnect:
                break
            if msg.get("type") != "websocket.receive":
                break
            with StateLock():
                state = read_state()
                if session_id in state:
                    state[session_id]["last_seen"] = int(time.time())
                    write_state(state)
            if "bytes" in msg and msg["bytes"] is not None:
                await asyncio.to_thread(channel.send, msg["bytes"])
            elif "text" in msg and msg["text"] is not None:
                text = msg["text"]
                try:
                    payload = json.loads(text)
                    if payload.get("type") == "input":
                        await asyncio.to_thread(channel.send, str(payload.get("data", "")).encode())
                    elif payload.get("type") == "resize":
                        rows, cols = clamp_terminal_size(
                            int(payload.get("rows", 24)), int(payload.get("cols", 80))
                        )
                        await asyncio.to_thread(channel.resize_pty, width=cols, height=rows)
                except json.JSONDecodeError:
                    await asyncio.to_thread(channel.send, text.encode())

    ws_task = asyncio.create_task(ws_to_pty())
    pty_task = asyncio.create_task(pty_to_ws())
    try:
        completed_tasks, pending_tasks = await asyncio.wait(
            {pty_task, ws_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending_tasks:
            task.cancel()
        await asyncio.gather(*pending_tasks, return_exceptions=True)
        for task in completed_tasks:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    finally:
        channel.close()
        client.close()
        # Keep sessions reconnectable until inactivity timeout instead of destroying on disconnect.
        with StateLock():
            state = read_state()
            if session_id in state and state[session_id].get("status") in {"creating", "running"}:
                state[session_id]["last_seen"] = int(time.time())
                write_state(state)
                if client_id:
                    upsert_session_history(client_id, session_id, state[session_id], int(time.time()))


@app.websocket("/ws-gui/{session_id}")
async def gui_session(websocket: WebSocket, session_id: str):
    """WebSocket endpoint that proxies binary RFB (VNC) traffic to x11vnc inside the container.

    noVNC in the browser connects here.  An SSH ``direct-tcpip`` channel tunnels the traffic
    from the Proxmox host to the container's VNC port (5900) without exposing it externally.
    """
    validate_session_id(session_id)
    await websocket.accept(subprotocol="binary")

    with StateLock():
        state = read_state()
        session = state.get(session_id)
        if not session or session.get("status") != "running" or not session.get("is_gui"):
            await websocket.close(code=1008)
            return
        session["last_seen"] = int(time.time())
        state[session_id] = session
        write_state(state)
        if session.get("client_id"):
            upsert_session_history(session["client_id"], session_id, session, int(time.time()))

    container_ip = str(session["ip"])
    client_id = str(session.get("client_id", ""))

    client = await asyncio.to_thread(open_ssh_client, 10)
    transport = client.get_transport()
    if transport is None:
        logger.error("Failed to open SSH transport for gui session_id=%s", session_id)
        client.close()
        await websocket.close(code=1011)
        return

    try:
        channel = await asyncio.to_thread(
            transport.open_channel,
            "direct-tcpip",
            (container_ip, 5900),
            ("127.0.0.1", 0),
        )
    except Exception as exc:
        logger.error("Failed to open VNC channel for session %s: %s", session_id, exc)
        client.close()
        await websocket.close(code=1011)
        return

    async def vnc_to_ws():
        while True:
            if await asyncio.to_thread(channel.recv_ready):
                data = await asyncio.to_thread(channel.recv, 16384)
                if not data:
                    break
                if (
                    websocket.application_state != WebSocketState.CONNECTED
                    or websocket.client_state != WebSocketState.CONNECTED
                ):
                    break
                try:
                    await websocket.send_bytes(data)
                except RuntimeError:
                    if (
                        websocket.application_state != WebSocketState.CONNECTED
                        or websocket.client_state != WebSocketState.CONNECTED
                    ):
                        break
                    raise
                continue
            if await asyncio.to_thread(lambda: channel.closed):
                break
            await asyncio.sleep(0.1)

    async def ws_to_vnc():
        while True:
            try:
                msg = await websocket.receive()
            except WebSocketDisconnect:
                break
            if msg.get("type") != "websocket.receive":
                break
            with StateLock():
                state = read_state()
                if session_id in state:
                    state[session_id]["last_seen"] = int(time.time())
                    write_state(state)
            data: bytes | None = None
            if "bytes" in msg and msg["bytes"] is not None:
                data = msg["bytes"]
            elif "text" in msg and msg["text"] is not None:
                data = msg["text"].encode()
            if data:
                await asyncio.to_thread(channel.send, data)

    ws_task = asyncio.create_task(ws_to_vnc())
    vnc_task = asyncio.create_task(vnc_to_ws())
    try:
        completed_tasks, pending_tasks = await asyncio.wait(
            {vnc_task, ws_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending_tasks:
            task.cancel()
        await asyncio.gather(*pending_tasks, return_exceptions=True)
        for task in completed_tasks:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    finally:
        channel.close()
        client.close()
        # Keep sessions reconnectable until inactivity timeout instead of destroying on disconnect.
        with StateLock():
            state = read_state()
            if session_id in state and state[session_id].get("status") in {"creating", "running"}:
                state[session_id]["last_seen"] = int(time.time())
                write_state(state)
                if client_id:
                    upsert_session_history(client_id, session_id, state[session_id], int(time.time()))


async def cleanup_loop():
    while True:
        try:
            cfg = load_config()
            cutoff = int(time.time()) - int(cfg["session_ttl_seconds"])
            state = read_state()
            stale = [
                sid
                for sid, session in state.items()
                if session.get("status") in {"creating", "running"} and int(session.get("last_seen", 0)) < cutoff
            ]
            for sid in stale:
                await cleanup_session(sid, "timeout")
        except Exception:
            pass
        await asyncio.sleep(30)


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port_raw = os.environ.get("PORT", "5000")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid PORT value: {port_raw}") from exc
    if not (1 <= port <= 65535):
        raise SystemExit(f"PORT out of range (1-65535): {port}")
    logger.info("Starting LXChoster in foreground on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port)
