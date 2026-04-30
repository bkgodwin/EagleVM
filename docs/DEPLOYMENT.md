# VM Classroom Deployment Guide

This guide assumes a local LAN, Proxmox VE, one Ubuntu LXC container for the web app and Guacamole, and Windows 11 VMs cloned from a Proxmox template.

## 1. Proxmox Setup

Create a Windows 11 VM template:

1. Upload a Windows 11 ISO and VirtIO drivers ISO to Proxmox.
2. Create a VM with UEFI, TPM 2.0, VirtIO SCSI, VirtIO network, and enough disk for student work.
3. Install Windows 11.
4. Install VirtIO drivers from the mounted ISO.
5. Install QEMU guest agent in Windows.
6. Enable the guest agent in Proxmox:

   ```bash
   qm set 9000 --agent enabled=1
   ```

7. Enable Remote Desktop in Windows:
   - Settings -> System -> Remote Desktop -> On
   - Allow the configured RDP user to log in.
   - Confirm Windows Firewall allows Remote Desktop.

8. Optimize for classroom use:
   - Disable sleep and hibernation.
   - Install required class software.
   - Run Windows Update.
   - Use a local account dedicated to lab use, for example `student`.
   - Set a fixed display resolution if your course software prefers one.
   - Shut down cleanly.

9. Convert the VM to a template:

   ```bash
   qm template 9000
   ```

The app names cloned VMs as `vm-{userid}` and allocates VM IDs as `PROXMOX_VM_ID_START + user_id`.

## 2. Proxmox API Token

Create a role with the required VM permissions:

```bash
pveum role add VMClassroomRole -privs "VM.Allocate VM.Clone VM.Config.CPU VM.Config.Memory VM.Config.Network VM.Monitor VM.PowerMgmt VM.Audit Datastore.AllocateSpace"
pveum user add vmclassroom@pve
pveum aclmod / -user vmclassroom@pve -role VMClassroomRole
pveum user token add vmclassroom@pve app --privsep 0
```

Put the token in `.env`:

```env
PROXMOX_API_TOKEN_ID=vmclassroom@pve!app
PROXMOX_API_TOKEN_SECRET=<secret shown once by Proxmox>
```

If your Proxmox certificate is self-signed, either install the CA certificate in the LXC container or set `PROXMOX_VERIFY_SSL=false` for a lab-only deployment.

## 3. Ubuntu LXC Setup

Create an Ubuntu 22.04 or 24.04 LXC on the same LAN as Proxmox and the VMs.

Install packages:

```bash
apt update
apt install -y python3 python3-venv python3-pip nginx mariadb-server tomcat9 tomcat9-admin \
  libcairo2 libjpeg-turbo8 libpng16-16 libossp-uuid16 libavcodec-extra freerdp2-x11 wget curl make gcc
```

Deploy the app:

```bash
mkdir -p /opt/vm-classroom
rsync -a ./ /opt/vm-classroom/
cd /opt/vm-classroom
python3 -m venv .venv
. .venv/bin/activate
pip install -r app/requirements.txt
cp .env.example .env
chmod 600 .env
```

Edit `.env` for your LAN.

Initialize the database by starting the app once:

```bash
. /opt/vm-classroom/.venv/bin/activate
export $(grep -v '^#' /opt/vm-classroom/.env | xargs)
flask --app app.app run --host 0.0.0.0 --port 8000
```

Stop it after confirming it starts.

## 4. Apache Guacamole Installation

Install guacd. On Ubuntu, use packages if available:

```bash
apt install -y guacd guacamole-tomcat
systemctl enable --now guacd tomcat9
```

If your Ubuntu release does not ship `guacamole-tomcat`, download matching Guacamole server/client releases from Apache and follow the Apache build instructions for `guacamole-server`.

Create the MariaDB database:

```bash
mysql
```

```sql
CREATE DATABASE guacamole_db;
CREATE USER 'guacamole_user'@'localhost' IDENTIFIED BY 'change-this-password';
GRANT SELECT,INSERT,UPDATE,DELETE ON guacamole_db.* TO 'guacamole_user'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

Load the Guacamole schema. The schema files are provided by the Guacamole auth JDBC extension:

```bash
cat schema/*.sql | mysql -u root guacamole_db
```

Configure `/etc/guacamole/guacamole.properties`:

```properties
guacd-hostname: localhost
guacd-port: 4822
mysql-hostname: localhost
mysql-database: guacamole_db
mysql-username: guacamole_user
mysql-password: change-this-password
```

Install the MySQL auth extension and connector/J under `/etc/guacamole/extensions` and `/etc/guacamole/lib`, then restart:

```bash
systemctl restart guacd tomcat9
```

Log into Guacamole once at `http://<lxc-ip>:8080/guacamole` with `guacadmin/guacadmin`, change the password, and put it in `.env` as `GUAC_ADMIN_PASSWORD`.

## 5. Connecting Guacamole to VMs

The web app creates and updates Guacamole RDP connections automatically when a student launches a VM.

Required `.env` values:

```env
GUAC_BASE_URL=http://127.0.0.1:8080/guacamole
GUAC_PUBLIC_URL=/guacamole
GUAC_ADMIN_USER=guacadmin
GUAC_ADMIN_PASSWORD=<changed-admin-password>
GUAC_RDP_USERNAME=student
GUAC_RDP_PASSWORD=<windows-student-password>
GUAC_SECURITY_MODE=any
GUAC_IGNORE_CERT=true
```

The app first tries to discover the VM IP address through the QEMU guest agent. If that fails, it uses the DNS hostname `vm-{userid}`. For best results, enable DHCP registration or create DNS records for VM names.

## 6. Configure the Web App

Important `.env` settings:

```env
SECRET_KEY=<generate with: openssl rand -hex 32>
PROXMOX_HOST=192.168.1.10
PROXMOX_NODE=pve
PROXMOX_TEMPLATE_VM_ID=9000
PROXMOX_VM_ID_START=2000
PROXMOX_STORAGE=local-lvm
DEFAULT_CPU=2
DEFAULT_RAM=4096
```

The admin dashboard can change defaults stored in SQLite. Secrets remain in environment variables and are never stored in the database.

## 7. Run with systemd

Create `/etc/systemd/system/vm-classroom.service`:

```ini
[Unit]
Description=VM Classroom Flask app
After=network-online.target mariadb.service guacd.service tomcat9.service
Wants=network-online.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/vm-classroom
EnvironmentFile=/opt/vm-classroom/.env
ExecStart=/opt/vm-classroom/.venv/bin/gunicorn -w 3 -b 127.0.0.1:8000 app.app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
chown -R www-data:www-data /opt/vm-classroom
systemctl daemon-reload
systemctl enable --now vm-classroom
```

Configure nginx at `/etc/nginx/sites-available/vm-classroom`:

```nginx
server {
    listen 80;
    server_name _;

    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /guacamole/ {
        proxy_pass http://127.0.0.1:8080/guacamole/;
        proxy_buffering off;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable:

```bash
ln -s /etc/nginx/sites-available/vm-classroom /etc/nginx/sites-enabled/vm-classroom
nginx -t
systemctl reload nginx
```

## 8. Troubleshooting

VM not starting:

- Check `journalctl -u vm-classroom -f`.
- Confirm the API token role includes `VM.PowerMgmt`.
- Run `qm status <vmid>` on Proxmox.
- Verify the cloned VM ID is not already in use.

Guacamole not connecting:

- Confirm RDP is enabled in Windows.
- From the LXC, run `xfreerdp /v:<vm-ip> /u:student /p:<password>`.
- Check `journalctl -u guacd -f`.
- Confirm `GUAC_RDP_USERNAME` and `GUAC_RDP_PASSWORD`.
- Confirm nginx proxies `/guacamole/` to Tomcat.

API auth errors:

- Confirm `PROXMOX_API_TOKEN_ID` format is `user@realm!token`.
- Confirm the token secret was copied exactly.
- If `PROXMOX_VERIFY_SSL=true`, verify the LXC trusts the Proxmox certificate.

VM IP not found:

- Confirm QEMU guest agent is installed and running in Windows.
- Confirm the VM config has `agent: enabled=1`.
- Use DNS names like `vm-12` as a fallback or reserve DHCP leases.

Students see the Guacamole login screen:

- Confirm `GUAC_PUBLIC_URL=/guacamole`.
- Confirm the Guacamole user can authenticate through the API.
- Check that browser cookies are allowed for the LAN site.
