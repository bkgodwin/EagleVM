#!/usr/bin/env bash
set -euo pipefail

PREFIX="${1:-vm-}"

if ! command -v qm >/dev/null 2>&1; then
  echo "Run this on a Proxmox node with qm available." >&2
  exit 1
fi

qm list | awk -v prefix="$PREFIX" 'NR > 1 && $2 ~ "^" prefix {print $1, $2}' | while read -r vmid name; do
  echo "Stopping and destroying $name ($vmid)"
  qm stop "$vmid" || true
  qm destroy "$vmid" --purge 1 --destroy-unreferenced-disks 1
done
