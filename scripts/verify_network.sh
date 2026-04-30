#!/usr/bin/env bash
set -euo pipefail

PROXMOX_HOST="${1:?usage: verify_network.sh <proxmox-host> <guacamole-url> [vm-ip]}"
GUAC_URL="${2:?usage: verify_network.sh <proxmox-host> <guacamole-url> [vm-ip]}"
VM_IP="${3:-}"

echo "Checking Proxmox API..."
curl -kfsS "https://${PROXMOX_HOST}:8006/api2/json/version" >/dev/null
echo "Proxmox API reachable."

echo "Checking Guacamole..."
curl -fsS "${GUAC_URL%/}/api/languages" >/dev/null
echo "Guacamole reachable."

if [[ -n "$VM_IP" ]]; then
  echo "Checking RDP port on $VM_IP..."
  timeout 5 bash -c "cat < /dev/null > /dev/tcp/${VM_IP}/3389"
  echo "RDP port reachable."
fi
