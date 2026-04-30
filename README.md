# VM Classroom

VM Classroom is a local-LAN Flask application for student access to persistent Windows 11 VMs hosted on Proxmox VE and streamed through Apache Guacamole.

Default first-run admin:

- Email: `admin@local`
- Password: `admin123`

The admin account is forced to change its password on first login.

## Quick Start

```bash
cd /opt/vm-classroom
python3 -m venv .venv
. .venv/bin/activate
pip install -r app/requirements.txt
cp .env.example .env
flask --app app.app run --host 0.0.0.0 --port 8000
```

Open `http://<lxc-ip>:8000`.

For production, follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
