import asyncio
import base64
import fcntl
import json
import logging
import os
import pty
import secrets
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("LXCHOSTER_CONFIG", BASE_DIR / "config.json"))
STATE_DIR = Path(os.environ.get("LXCHOSTER_STATE_DIR", "/var/lib/lxchoster"))
STATE_PATH = STATE_DIR / "sessions.json"
LOCK_PATH = STATE_DIR / "sessions.lock"

app = FastAPI(title="LXChoster")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lxchoster")


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open() as fh:
        cfg = json.load(fh)
    cfg.setdefault("ssh_user", "root")
    cfg.setdefault("ssh_key_path", "/opt/lxchoster/.ssh/id_ed25519")
    cfg.setdefault("storage", "local-lvm")
    cfg.setdefault("bridge", "vmbr0")
    cfg.setdefault("max_sessions", 25)
    cfg.setdefault("session_ttl_seconds", 3600)
    cfg.setdefault("boot_timeout_seconds", 90)
    return cfg


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


def active_session_count(state: dict[str, Any]) -> int:
    """Return the number of currently active sessions (creating/running)."""
    return sum(1 for session in state.values() if session.get("status") in {"creating", "running"})


def ssh_cmd(remote: str, timeout: int = 60) -> str:
    cfg = load_config()
    cmd = [
        "ssh",
        "-i",
        cfg["ssh_key_path"],
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        f"{cfg['ssh_user']}@{cfg['proxmox_host']}",
        remote,
    ]
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"command failed: {remote}")
    return result.stdout.strip()


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


def resize_pty(fd: int, rows: int, cols: int) -> None:
    rows = max(10, min(int(rows), 200))
    cols = max(20, min(int(cols), 400))
    packed = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE_DIR / "static" / "index.html").read_text()


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

    cfg = load_config()
    vmid = int(session["vmid"])
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(
            "ssh",
            [
                "ssh",
                "-tt",
                "-i",
                cfg["ssh_key_path"],
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=no",
                f"{cfg['ssh_user']}@{cfg['proxmox_host']}",
                f"pct exec {vmid} -- bash -l",
            ],
        )

    loop = asyncio.get_running_loop()

    async def pty_to_ws():
        while True:
            data = await loop.run_in_executor(None, os.read, fd, 4096)
            if not data:
                break
            await websocket.send_bytes(data)

    async def ws_to_pty():
        while True:
            msg = await websocket.receive()
            with StateLock():
                state = read_state()
                if session_id in state:
                    state[session_id]["last_seen"] = int(time.time())
                    write_state(state)
            if "bytes" in msg and msg["bytes"] is not None:
                os.write(fd, msg["bytes"])
            elif "text" in msg and msg["text"] is not None:
                text = msg["text"]
                try:
                    payload = json.loads(text)
                    if payload.get("type") == "input":
                        os.write(fd, str(payload.get("data", "")).encode())
                    elif payload.get("type") == "resize":
                        resize_pty(fd, int(payload.get("rows", 24)), int(payload.get("cols", 80)))
                except json.JSONDecodeError:
                    os.write(fd, text.encode())

    try:
        await asyncio.gather(pty_to_ws(), ws_to_pty())
    except WebSocketDisconnect:
        pass
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        os.close(fd)
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


@app.on_event("startup")
async def startup():
    init_state()
    asyncio.create_task(cleanup_loop())


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
