# LXChoster

LXChoster is a FastAPI/xterm.js web app that creates temporary Proxmox LXC sessions from a template.
Users press Launch to create a temporary root shell session. The app generates an internal random root password for the temporary container before attaching the browser terminal.
On first startup, the app prompts for the Proxmox root password and stores an encrypted value in `config.json` for subsequent runs.

## Config

Edit `/opt/lxchoster/config.json`.

- `template_id`: Proxmox LXC template VMID to clone.
- `proxmox_root_password_encrypted`: encrypted Proxmox root password. Leave blank initially to be prompted at startup.
- `ssh_known_hosts_path`: path to known_hosts file used to verify the Proxmox SSH host key.
- `max_sessions`: maximum concurrent temporary sessions. Default here is `25`.
- `session_ttl_seconds`: reconnect window / inactivity timeout before cleanup. Default is `600` (10 minutes).
- `local_network_blocklist`: CIDRs blocked from container egress to limit local network access.
- `local_network_allowlist`: CIDRs allowed before block rules are applied.

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

By default this listens on `0.0.0.0:5000`. Override with `HOST=<host>` and/or `PORT=<port>`.
If the host is reachable from untrusted networks, place it behind a firewall/reverse proxy or set `HOST=127.0.0.1`.
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

Temporary containers are named `temp-<sessionid>`. Browser reconnects from the same client are attached back to the same running session while inside `session_ttl_seconds`. A background cleanup loop scans persisted session state and destroys stale active containers after `session_ttl_seconds`, covering app restarts and abandoned sessions. User-to-session mappings are persisted in `/var/lib/lxchoster/session_history.csv` and removed when session cleanup destroys the container.
