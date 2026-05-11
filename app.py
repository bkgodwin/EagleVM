import asyncio
import base64
import fcntl
import getpass
import json
import logging
import os
import socket
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import paramiko
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("LXCHOSTER_CONFIG", BASE_DIR / "config.json"))
STATE_DIR = Path(os.environ.get("LXCHOSTER_STATE_DIR", "/var/lib/lxchoster"))
STATE_PATH = STATE_DIR / "sessions.json"
LOCK_PATH = STATE_DIR / "sessions.lock"
SECRET_KEY_PATH = STATE_DIR / "secret.key"
PROXMOX_PASSWORD_KEY = "proxmox_root_password_encrypted"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_state()
    await asyncio.to_thread(ensure_proxmox_password)
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
    cfg.setdefault("session_ttl_seconds", 3600)
    cfg.setdefault("boot_timeout_seconds", 90)
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


def decrypt_secret(value: str) -> str:
    try:
        return get_fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Failed to decrypt stored Proxmox root password. "
            "If needed, clear proxmox_root_password_encrypted in config.json and restart to re-enter it."
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


def ensure_proxmox_password() -> str:
    global _PROXMOX_PASSWORD_CACHE
    if _PROXMOX_PASSWORD_CACHE is not None:
        return _PROXMOX_PASSWORD_CACHE

    cfg = load_config()
    encrypted = cfg.get(PROXMOX_PASSWORD_KEY, "").strip()
    if encrypted:
        password = decrypt_secret(encrypted)
    else:
        password = prompt_for_proxmox_password()
        cfg[PROXMOX_PASSWORD_KEY] = encrypt_secret(password)
        save_config(cfg)
        logger.info("Encrypted Proxmox root password stored in %s", CONFIG_PATH)
    _PROXMOX_PASSWORD_CACHE = password
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
async def launch():
    cfg = load_config()
    password = secrets.token_urlsafe(24)
    now = int(time.time())
    with StateLock():
        state = read_state()
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
            "status": "creating",
            "created_at": now,
            "last_seen": now,
        }
        write_state(state)

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
        ip = await wait_for_container(vmid, int(cfg["boot_timeout_seconds"]))
    except Exception as exc:
        await cleanup_session(session_id, f"create_failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    with StateLock():
        state = read_state()
        state[session_id].update({"status": "running", "ip": ip, "last_seen": int(time.time())})
        write_state(state)
        logger.info(
            "Session created id=%s vmid=%s hostname=%s active_sessions=%d",
            session_id,
            vmid,
            hostname,
            active_session_count(state),
        )
    return {"session_id": session_id, "vmid": vmid, "hostname": hostname, "ip": ip}


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

    vmid = int(session["vmid"])
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
                try:
                    await websocket.send_bytes(data)
                except RuntimeError:
                    break
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
            if msg["type"] == "websocket.disconnect":
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
        done, pending = await asyncio.wait({pty_task, ws_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, RuntimeError)):
                raise exc
    except WebSocketDisconnect:
        pass
    finally:
        channel.close()
        client.close()
        await cleanup_session(session_id, "websocket_disconnect")


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
    host = os.environ.get("HOST", "127.0.0.1")
    port_raw = os.environ.get("PORT", "8000")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid PORT value: {port_raw}") from exc
    if not (1 <= port <= 65535):
        raise SystemExit(f"PORT out of range (1-65535): {port}")
    logger.info("Starting LXChoster in foreground on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port)
