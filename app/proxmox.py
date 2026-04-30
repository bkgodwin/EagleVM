import time
from dataclasses import dataclass
from typing import Optional

import requests
from flask import current_app


class ProxmoxError(RuntimeError):
    pass


@dataclass
class VMIdentity:
    vm_id: int
    name: str


class ProxmoxClient:
    def __init__(self):
        cfg = current_app.config
        from . import db
        from .models import Settings

        settings = db.session.get(Settings, 1)
        self.host = (settings.proxmox_host if settings and settings.proxmox_host else cfg["PROXMOX_HOST"]).rstrip("/")
        self.node = settings.proxmox_node if settings and settings.proxmox_node else cfg["PROXMOX_NODE"]
        self.verify_ssl = cfg["PROXMOX_VERIFY_SSL"]
        self.token_id = cfg["PROXMOX_API_TOKEN_ID"]
        self.token_secret = cfg["PROXMOX_API_TOKEN_SECRET"]
        if not all([self.host, self.node, self.token_id, self.token_secret]):
            raise ProxmoxError("Proxmox configuration is incomplete.")
        self.base = f"https://{self.host}:8006/api2/json"
        self.session = requests.Session()
        self.session.verify = self.verify_ssl
        self.session.headers.update(
            {"Authorization": f"PVEAPIToken={self.token_id}={self.token_secret}"}
        )

    def _request(self, method, path, **kwargs):
        url = f"{self.base}{path}"
        for attempt in range(1, 4):
            try:
                response = self.session.request(method, url, timeout=30, **kwargs)
                response.raise_for_status()
                payload = response.json()
                return payload.get("data")
            except requests.RequestException as exc:
                if attempt == 3:
                    raise ProxmoxError(f"Proxmox API request failed: {exc}") from exc
                time.sleep(attempt * 2)

    def _wait_for_task(self, upid, timeout=900):
        deadline = time.time() + timeout
        encoded = requests.utils.quote(upid, safe="")
        while time.time() < deadline:
            data = self._request("GET", f"/nodes/{self.node}/tasks/{encoded}/status")
            if data.get("status") == "stopped":
                exitstatus = data.get("exitstatus")
                if exitstatus == "OK":
                    return
                raise ProxmoxError(f"Proxmox task failed with exit status {exitstatus}.")
            time.sleep(3)
        raise ProxmoxError("Timed out waiting for Proxmox task.")

    def clone_vm(self, template_vm_id, target: VMIdentity, cpu, ram):
        params = {
            "newid": target.vm_id,
            "name": target.name,
            "full": 1 if current_app.config["PROXMOX_FULL_CLONE"] else 0,
        }
        if current_app.config["PROXMOX_STORAGE"]:
            params["storage"] = current_app.config["PROXMOX_STORAGE"]
        upid = self._request(
            "POST",
            f"/nodes/{self.node}/qemu/{template_vm_id}/clone",
            data=params,
        )
        self._wait_for_task(upid)
        self.configure_vm(target.vm_id, cpu=cpu, ram=ram)

    def configure_vm(self, vm_id, cpu, ram):
        params = {"cores": int(cpu), "memory": int(ram), "agent": "enabled=1"}
        if current_app.config["PROXMOX_VM_BRIDGE"]:
            params["net0"] = f"virtio,bridge={current_app.config['PROXMOX_VM_BRIDGE']}"
        self._request("POST", f"/nodes/{self.node}/qemu/{vm_id}/config", data=params)

    def start_vm(self, vm_id):
        upid = self._request("POST", f"/nodes/{self.node}/qemu/{vm_id}/status/start")
        self._wait_for_task(upid, timeout=300)

    def stop_vm(self, vm_id):
        upid = self._request("POST", f"/nodes/{self.node}/qemu/{vm_id}/status/stop")
        self._wait_for_task(upid, timeout=300)

    def destroy_vm(self, vm_id):
        upid = self._request(
            "DELETE",
            f"/nodes/{self.node}/qemu/{vm_id}",
            params={"purge": 1, "destroy-unreferenced-disks": 1},
        )
        self._wait_for_task(upid, timeout=600)

    def status(self, vm_id):
        data = self._request("GET", f"/nodes/{self.node}/qemu/{vm_id}/status/current")
        return data.get("status", "unknown")

    def guest_ip(self, vm_id) -> Optional[str]:
        try:
            data = self._request("GET", f"/nodes/{self.node}/qemu/{vm_id}/agent/network-get-interfaces")
        except ProxmoxError:
            return None
        for iface in data.get("result", []):
            for addr in iface.get("ip-addresses", []):
                ip = addr.get("ip-address")
                if addr.get("ip-address-type") == "ipv4" and ip and not ip.startswith("127."):
                    return ip
        return None


def allocate_vm_id(user_id):
    return current_app.config["PROXMOX_VM_ID_START"] + int(user_id)
