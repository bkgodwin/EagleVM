# LXChoster

LXChoster is a FastAPI/xterm.js web app that creates temporary Proxmox LXC sessions from a template.
Users press Launch to create a temporary root shell session. The app generates an internal random root password for the temporary container before attaching the browser terminal, and the web UI now shows live startup phases plus a progress bar while the guest is being cloned and booted.
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

### GUI mode

Set `"is_gui": true` to switch all sessions from a root terminal to a full graphical desktop streamed through the browser. Keyboard and mouse input are forwarded to the VM in real time via noVNC.

Additional config keys for GUI mode:

- `is_gui` *(boolean, default `false`)*: when `true` each session starts a graphical desktop (Xvfb + desktop environment + x11vnc) instead of a plain shell.
- `gui_vm_username`: OS username that users will log into inside the desktop environment. Leave blank if not applicable.
- `gui_vm_password_encrypted`: encrypted password for `gui_vm_username`. Leave blank on first startup when `is_gui` is `true` to be prompted interactively; the encrypted value is then stored automatically.

The credentials are displayed to the user in a popup dialog when the session launches, and remain visible in the bottom info bar throughout the session.

#### Template requirements for GUI mode

The container template cloned for GUI sessions must have the following packages installed:

- `xvfb` — X virtual framebuffer
- `x11vnc` — VNC server for the virtual display
- At least one desktop environment, e.g. `xfce4`, `openbox`, or `fluxbox`

The VNC server is started on port 5900 inside the container and is only accessible via an SSH `direct-tcpip` tunnel from the Proxmox host. It is never exposed to external networks directly.

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

Temporary containers are named `temp-<sessionid>`. Browser reconnects from the same client are attached back to the same running session while inside `session_ttl_seconds`. A background cleanup loop scans persisted session state and destroys stale active containers after `session_ttl_seconds`, covering app restarts and abandoned sessions. User-to-session mappings are persisted in `/var/lib/lxchoster/session_history.csv` and removed when session cleanup destroys the container. In GUI mode the container (including all GUI processes) is destroyed on cleanup just like any other session.
