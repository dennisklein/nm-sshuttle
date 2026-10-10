#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Provision a fresh Fedora 44 Cloud VM for the spike. vm.sh runs this as
# root inside the VM, then reboots it.
#
#   provision.sh [--user NAME] [--headless] [--sshuttle-pip VERSION]
#
# Default: install the spike's dependencies and the GNOME Workstation
# environment, and log NAME in automatically on the VM's display.
# --headless skips GNOME (the manual GNOME and agent checks are then skipped).
# --sshuttle-pip installs that sshuttle version from PyPI into a venv instead
# of using Fedora's package.
set -euo pipefail

USER_NAME=tester
GUI=1
SSHUTTLE_PIP=""
while [ $# -gt 0 ]; do
    case $1 in
        --user) USER_NAME=$2; shift 2 ;;
        --headless) GUI=0; shift ;;
        --sshuttle-pip) SSHUTTLE_PIP=$2; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

step() { echo; echo "### $*"; }

step "waiting for cloud-init"
cloud-init status --wait > /dev/null 2>&1 || true

step "updating the system (the new kernel and kernel-modules match after the reboot)"
dnf -y upgrade --refresh

step "installing the spike's dependencies"
dnf -y install sshuttle nftables iproute openssh-server openssh-clients \
    python3-gobject-base NetworkManager systemd-resolved curl audit \
    policycoreutils kernel-modules util-linux procps-ng
systemctl enable --now auditd systemd-resolved

if [ -n "$SSHUTTLE_PIP" ]; then
    step "installing sshuttle $SSHUTTLE_PIP from PyPI"
    python3 -m venv /opt/sshuttle-venv
    /opt/sshuttle-venv/bin/pip install --quiet "sshuttle==$SSHUTTLE_PIP"
    ln -sf /opt/sshuttle-venv/bin/sshuttle /usr/local/bin/sshuttle
    restorecon -RF /opt/sshuttle-venv /usr/local/bin/sshuttle || true
fi

if [ "$GUI" = 1 ]; then
    step "installing GNOME (Fedora Workstation environment; this is the slow part)"
    dnf -y install @workstation-product-environment ||
        dnf -y group install workstation-product-environment

    step "logging $USER_NAME in automatically, without screen lock or first-run wizard"
    cat > /etc/gdm/custom.conf <<EOF
[daemon]
AutomaticLoginEnable=True
AutomaticLogin=$USER_NAME
EOF
    mkdir -p /etc/dconf/profile /etc/dconf/db/local.d
    if [ ! -f /etc/dconf/profile/user ]; then
        printf 'user-db:user\nsystem-db:local\n' > /etc/dconf/profile/user
    elif ! grep -q '^system-db:local' /etc/dconf/profile/user; then
        echo 'system-db:local' >> /etc/dconf/profile/user
    fi
    cat > /etc/dconf/db/local.d/00-nmss-spike <<'EOF'
[org/gnome/desktop/screensaver]
lock-enabled=false

[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
EOF
    dconf update
    home=$(getent passwd "$USER_NAME" | cut -d: -f6)
    install -d -o "$USER_NAME" -g "$(id -g "$USER_NAME")" "$home/.config"
    echo yes > "$home/.config/gnome-initial-setup-done"
    chown "$USER_NAME:" "$home/.config/gnome-initial-setup-done"
    systemctl set-default graphical.target
else
    systemctl set-default multi-user.target
fi

echo "$(date -Is) gui=$GUI sshuttle=${SSHUTTLE_PIP:-fedora}" > /var/lib/nmss-spike-provisioned
step "provisioned; reboot to finish"
