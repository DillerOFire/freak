#!/usr/bin/env bash
set -euo pipefail

readonly source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly install_root='/usr/local/lib/freak-release-updater'

[[ "${EUID}" -eq 0 ]] || { printf '%s\n' 'Run this installer as root.' >&2; exit 1; }
install -d -o root -g root -m 0755 "$install_root"
install -o root -g root -m 0755 "$source_root/freak_release_updater.py" "$install_root/freak_release_updater.py"
install -o root -g root -m 0644 "$source_root/freak-release-updater.service" /etc/systemd/system/freak-release-updater.service
install -o root -g root -m 0644 "$source_root/freak-release-updater.timer" /etc/systemd/system/freak-release-updater.timer
install -d -o root -g root -m 0700 /var/lib/freak-release-updater/compose
systemctl daemon-reload
systemctl enable --now freak-release-updater.timer
