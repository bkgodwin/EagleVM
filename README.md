# LXChoster

LXChoster is a FastAPI/xterm.js web app that creates temporary Proxmox LXC sessions from a template.
Users press Launch to create a temporary root shell session. The app generates an internal random root password for the temporary container before attaching the browser terminal.

## Config

Edit `/opt/lxchoster/config.json`.

- `template_id`: Proxmox LXC template VMID to clone.
- `max_sessions`: maximum concurrent temporary sessions. Default here is `25`.
- `session_ttl_seconds`: inactivity timeout before cleanup.

## Runtime

- App path: `/opt/lxchoster`
- State path: `/var/lib/lxchoster/sessions.json`
- Service: `lxchoster.service`
- Foreground CLI run: `python3 app.py`

### Run in foreground (no service)

```sh
cd /opt/lxchoster
python3 app.py
```

By default this listens on `127.0.0.1:8000`. Override with `HOST=<host>` and/or `PORT=<port>`.
Session lifecycle events are logged to the terminal, including active session count on create/delete.

Restart:

```sh
systemctl restart lxchoster
```

Logs:

```sh
journalctl -u lxchoster -f
```

## Cleanup

Temporary containers are named `temp-<sessionid>`. The websocket disconnect handler shuts down and destroys the session container. A background cleanup loop scans persisted session state and destroys stale active containers after `session_ttl_seconds`, covering app restarts and abandoned sessions.
