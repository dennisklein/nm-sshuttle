#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Build and install the plugin the way a packager would, as root in the VM
# (test-vm/vm.sh runs this from the copied repository):
#
#   meson setup build --prefix=/usr && meson install -C build
#   nm-sshuttle post-install
set -euo pipefail

SRC=$(cd "$(dirname "$0")/.." && pwd)
cd "$SRC"
rm -rf build
meson setup build --prefix=/usr
meson install -C build
systemctl --no-pager daemon-reload
# D-Bus must see the new policy and the activation file
busctl --system --no-pager call org.freedesktop.DBus /org/freedesktop/DBus \
    org.freedesktop.DBus ReloadConfig > /dev/null 2>&1 ||
    systemctl --no-pager reload dbus-broker 2> /dev/null || true
/usr/libexec/nm-sshuttle/nm-sshuttle post-install
echo "installed from $SRC"
