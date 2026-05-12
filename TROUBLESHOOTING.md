# Troubleshooting: VMs / Containers Not Starting

## Quick symptom guide

| What you see in the log | Likely cause |
|---|---|
| Repeated `SSH Connected … Authentication successful` every ~2 seconds for 90 s, then nothing | Container/VM boots but never gets an IP – `wait_for_container` or `wait_for_vm` polling loop timed out |
| `Session XXX creation failed: container … did not boot with an IPv4 address` | LXC container started but DHCP is not assigning an address |
| `Session XXX creation failed: VM … did not boot with a routable IPv4 address` | QEMU VM started but guest agent unreachable *and* MAC not visible in `ip neigh` |
| `Session XXX creation failed: remote command failed with exit code …` | A Proxmox CLI command (`pct clone`, `pct start`, `qm clone`, etc.) returned non-zero |
| `Session XXX creation failed: SSH connection … failed` | Cannot reach the Proxmox host over SSH at the point of that command |
| Session launches but GUI canvas stays blank / disconnects quickly | VNC inside VM is running, but tunnel traffic is being interrupted (most commonly by guest OUTPUT block rules to local subnets) |

---

## Enabling verbose (DEBUG) logging

Run with `LOG_LEVEL=DEBUG` to print every SSH command sent, its output, and each
poll-loop iteration result:

```sh
LOG_LEVEL=DEBUG python3 app.py
```

Or, if running as a service:

```ini
# /opt/lxchoster/systemd/lxchoster.service  (or wherever the unit file lives)
[Service]
Environment="LOG_LEVEL=DEBUG"
```

```sh
systemctl daemon-reload && systemctl restart lxchoster
journalctl -u lxchoster -f
```

With `DEBUG` logging you will see lines like:

```
DEBUG SSH cmd (timeout=15s): pct exec 8001 -- hostname -I
DEBUG Container 8001 poll #1: hostname -I returned empty (no IP yet)
DEBUG Container 8001 poll #2 error: remote command failed with exit code 1
```

This immediately tells you *which step* is failing and *why*.

---

## VM GUI blank display / immediate disconnect

If VM session creation succeeds but GUI streaming fails, collect the new GUI tunnel logs:

- `GUI tunnel setup session_id=... target=<vm_ip>:5900 ...`
- `GUI tunnel channel opened session_id=...`
- `GUI tunnel completed ... first_disconnect_side=... reason=...`
- `GUI tunnel teardown ...`

Interpretation:

- `Failed to open VNC channel ...` means tunnel setup failed before display traffic started.
- `first_disconnect_side=ssh` usually means the VM-side VNC channel closed first (check x11vnc inside VM).
- `first_disconnect_side=websocket` usually means browser/socket side closed first.

In VM GUI mode, ensure the VM can send return traffic back to the Proxmox host. If you block
`192.168.0.0/16` (or similar) in OUTPUT rules, add an allow entry for the Proxmox host IP first.
The app now auto-adds the Proxmox host's routed source IPv4 (`ip route get <vm_ip>`) and any
resolved `<proxmox_host>/32` entries for GUI VMs, so built-in OUTPUT filtering should not block
the VNC tunnel's return traffic.

---

## Step-by-step diagnosis

### 1 – Verify the Proxmox host is reachable

```sh
ssh root@<proxmox_host>
```

The app should already be connecting (you see "Authentication successful" in the log), so this
is just a quick sanity check. If SSH fails, verify `proxmox_host` in `config.json`.

### 2 – Verify the template exists

```sh
# On the Proxmox host:
pct list        # for LXC containers  (is_vm: false)
qm list         # for QEMU VMs        (is_vm: true)
```

Confirm that the VMID set in `config.json` under `template_id` (default `9000`) appears in
the list and is **not** running (it must be a stopped template).

### 3 – Try a manual clone + start

Run these commands *on the Proxmox host* (substitute `9000` / `8001` / `vmbr0` / `local-lvm`
as needed):

**LXC mode (`is_vm: false`)**:
```sh
pct clone 9000 8001 --hostname test-manual --full 1 --storage local-lvm
pct set 8001 --net0 name=eth0,bridge=vmbr0,ip=dhcp --features nesting=1
pct start 8001
# Wait ~10 s then:
pct exec 8001 -- hostname -I
```

Expected: an IPv4 address is printed.  If nothing is printed, see §4 below.

**VM mode (`is_vm: true`)**:
```sh
qm clone 9000 8001 --name test-manual --full 1
qm set 8001 --net0 model=virtio,bridge=vmbr0,firewall=0
qm start 8001
# Wait ~30 s then:
qm agent 8001 network-get-interfaces   # requires guest agent
# OR:
ip neigh show | grep -i <MAC from: qm config 8001 | grep net0>
```

Expected: an IP address appears.

### 4 – DHCP / network not assigning an IP (most common failure)

When `pct exec <vmid> -- hostname -I` returns empty, the container has no IP. Common causes:

| Cause | Fix |
|---|---|
| `bridge` in `config.json` does not match a bridge that exists on the Proxmox host | Run `brctl show` on the host; update `bridge` in `config.json` |
| Template has no DHCP client (`dhclient`, `udhcpc`, etc.) | Install a DHCP client in the template: `pct exec 9000 -- apt-get install -y isc-dhcp-client` |
| Template network config hard-codes a static IP | Remove or comment out any static IP config inside the template (`/etc/network/interfaces`, `/etc/netplan/*.yaml`) |
| The container OS uses `systemd-networkd` but the template has no `.network` file for `eth0` | Add a matching networkd unit or switch to `NetworkManager`/`ifupdown` inside the template |
| IP range exhausted on the bridge / DHCP server | Check your DHCP server's lease table |

Quick test inside a running container:
```sh
pct exec 8001 -- dhclient eth0
pct exec 8001 -- hostname -I
```

### 5 – Guest agent missing or not running (VM mode only)

If `qm agent 8001 network-get-interfaces` returns an error like `QEMU guest agent is not running`,
the guest agent (qemu-guest-agent) is not installed or not started inside the VM template.

Fix:
```sh
qm start 9000   # start the template itself temporarily
# SSH or console into the template VM then:
apt-get install -y qemu-guest-agent
systemctl enable --now qemu-guest-agent
# Shutdown and mark as template again:
qm shutdown 9000
qm template 9000
```

The app will also fall back to ARP lookup (see `wait_for_vm` in `app.py`), so even without the
guest agent it should work *if* the VM appears in `ip neigh` on the Proxmox host.

### 6 – Bridge `vmbr0` does not exist or has no uplink

```sh
# On the Proxmox host:
brctl show
ip link show vmbr0
```

If the bridge is missing, create it in Proxmox's network configuration
(Datacenter → Node → Network → Create → Linux Bridge).

### 7 – Storage for full clone is full or wrong

```sh
# On the Proxmox host:
pvesm status
```

Make sure `storage` in `config.json` exists and has enough free space for a full clone.
`local-lvm` is the default; adjust if your template is on a different storage pool.

### 8 – Session stuck in "creating" state from a previous crash

If the app crashed mid-session, the session state file may show `"status": "creating"` for a
container/VM that no longer exists.  The cleanup loop will normally remove these after
`session_ttl_seconds` (default 600 s).  To clean up immediately:

```sh
# Edit /var/lib/lxchoster/sessions.json and remove or mark the stuck entry, then:
systemctl restart lxchoster
```

Or simply wait for the TTL.

---

## Adding more verbose logging for a future session

If the above steps don't identify the problem, the next session should be started with:

```sh
LOG_LEVEL=DEBUG python3 app.py 2>&1 | tee /tmp/lxchoster-debug.log
```

Then trigger a session launch from the web UI.  The debug log will contain:

- Every SSH command sent to Proxmox (command string, exit code, stdout, stderr)
- Every poll iteration of the boot-wait loop (what `hostname -I` / guest-agent returned)
- The exact exception and traceback when session creation fails

Share `/tmp/lxchoster-debug.log` for further analysis.

---

## Further troubleshooting steps (for next session)

If the problem persists after the above, consider the following additional instrumentation:

1. **Capture Proxmox task log** after a failed attempt:
   ```sh
   # On Proxmox host – find the task log for the most recent clone:
   journalctl -u pvedaemon --since "5 minutes ago"
   # or look in /var/log/pve/tasks/
   ls -lt /var/log/pve/tasks/ | head -5
   cat /var/log/pve/tasks/<most-recent-file>
   ```

2. **Packet capture on the bridge** to verify DHCP traffic:
   ```sh
   tcpdump -i vmbr0 -n port 67 or port 68
   ```
   You should see a DHCP Discover from the container MAC within a few seconds of `pct start`.

3. **Check container/VM console directly**:
   ```sh
   pct console 8001   # LXC
   # or
   qm terminal 8001   # QEMU (serial console)
   ```
   Look for kernel/init errors or network configuration errors during boot.

4. **Test `pct exec` / `qm guest exec` manually** right after start to confirm the exec mechanism works:
   ```sh
   pct exec 8001 -- id
   qm guest exec 8001 -- id
   ```

5. **Increase `boot_timeout_seconds`** in `config.json` if the container is simply slow to boot
   (e.g., running on HDD storage or a busy host).  The default is 90 s; try 180 s.

6. **Verify `is_vm` is set correctly** in `config.json`:
   - `"is_vm": false` → expects Proxmox LXC containers (`pct` commands)
   - `"is_vm": true` → expects QEMU VMs (`qm` commands)
   Using the wrong mode will cause every `pct`/`qm` command to fail with "not found" or
   "does not exist" errors.
