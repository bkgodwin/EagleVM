#!/usr/bin/env bash
set -euo pipefail

VMID="${1:?usage: prepare_windows_template.sh <vmid>}"

if ! command -v qm >/dev/null 2>&1; then
  echo "Run this on a Proxmox node with qm available." >&2
  exit 1
fi

qm set "$VMID" --agent enabled=1
qm set "$VMID" --tablet 1
qm set "$VMID" --boot order=scsi0
qm set "$VMID" --name "win11-template"

echo "Before converting to template, boot Windows and verify:"
echo "  - QEMU guest agent service is installed and running"
echo "  - RDP is enabled"
echo "  - Windows Firewall allows RDP"
echo "  - Classroom software is installed"
echo "Then shut down the VM and run: qm template $VMID"
