#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Run the nm-sshuttle M1 spike in a throwaway Fedora 44 VM, from a Fedora 44
# host. No libvirt and no root needed: plain QEMU/KVM with user-mode
# networking. The VM's SSH and VNC ports listen on 127.0.0.1 only.
#
#   spike/vm.sh [COMMAND] [OPTIONS]
#
# Commands:
#   all       (default) up + run
#   up        download and verify the Fedora 44 Cloud image, create and boot
#             the VM, provision it (GNOME unless --headless), reboot
#   run       copy the spike in, run it, copy the report back
#   viewer    open the VM's display (remote-viewer, VNC)
#   ssh       open a shell in the VM (or run: vm.sh ssh -- CMD...)
#   status    show whether the VM is running
#   down      shut the VM down (keeps its disk)
#   destroy   shut down and delete the VM (keeps the downloaded image)
#
# Options:
#   --headless            no GNOME: faster, but the manual checks are skipped
#   --auto                skip the manual GNOME checks even with GNOME
#   --sshuttle-pip VER    test sshuttle VER from PyPI instead of Fedora's package
#   --mem MB              VM memory (default 4096)
#   --cpus N              VM CPUs (default 4)
#   --disk GB             VM disk size (default 30)
#   --ssh-port PORT       host port forwarded to the VM's SSH (default 2244)
#   --vnc-display N       VNC display on 127.0.0.1 (default 44 = port 5944)
#   --image FILE          use this Fedora 44 Cloud qcow2 instead of downloading
#   --mirror URL          Fedora mirror base (default download.fedoraproject.org)
#   --workdir DIR         state directory (default ~/.cache/nm-sshuttle-spike)
#
# Host packages: sudo dnf install qemu-kvm qemu-img xorriso openssh-clients \
#                curl gnupg2 virt-viewer
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
RELEASE=44
ARCH=x86_64
VM_USER=tester
VM_PASSWORD=spike

CMD=all
GUI=1
AUTO=0
SSHUTTLE_PIP=""
MEM=4096
CPUS=4
DISK_GB=30
SSH_PORT=2244
VNC_DISPLAY=44
IMAGE=""
MIRROR=https://download.fedoraproject.org/pub/fedora/linux
WORKDIR=${XDG_CACHE_HOME:-$HOME/.cache}/nm-sshuttle-spike
SSH_EXTRA=()

case ${1:-} in
    all|up|run|viewer|ssh|status|down|destroy) CMD=$1; shift ;;
esac
while [ $# -gt 0 ]; do
    case $1 in
        --headless) GUI=0; shift ;;
        --auto) AUTO=1; shift ;;
        --sshuttle-pip) SSHUTTLE_PIP=$2; shift 2 ;;
        --mem) MEM=$2; shift 2 ;;
        --cpus) CPUS=$2; shift 2 ;;
        --disk) DISK_GB=$2; shift 2 ;;
        --ssh-port) SSH_PORT=$2; shift 2 ;;
        --vnc-display) VNC_DISPLAY=$2; shift 2 ;;
        --image) IMAGE=$(realpath "$2"); shift 2 ;;
        --mirror) MIRROR=$2; shift 2 ;;
        --workdir) WORKDIR=$2; shift 2 ;;
        --) shift; SSH_EXTRA=("$@"); break ;;
        -h|--help) sed -n '3,33p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
    esac
done

VMDIR=$WORKDIR/vm
IMGDIR=$WORKDIR/images
KEY=$VMDIR/id_ed25519
PIDFILE=$VMDIR/qemu.pid

say() { echo "==> $*"; }
die() { echo "error: $*" >&2; exit 1; }

# Settings chosen at "up" stick to the VM; later commands reuse them.
if [ -f "$VMDIR/vm.env" ] && [ "$CMD" != up ] && [ "$CMD" != all ]; then
    # shellcheck disable=SC1091
    . "$VMDIR/vm.env"
fi

# ------------------------------------------------------------------ host
check_host() {
    local missing=() c
    for c in qemu-system-$ARCH qemu-img xorriso ssh ssh-keygen curl sha256sum; do
        command -v "$c" > /dev/null || missing+=("$c")
    done
    [ ${#missing[@]} = 0 ] || die "missing: ${missing[*]}
  sudo dnf install qemu-kvm qemu-img xorriso openssh-clients curl gnupg2 virt-viewer"
    [ "$(uname -m)" = "$ARCH" ] || die "this script drives an $ARCH VM; host is $(uname -m)"
    if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
        ACCEL=(-accel kvm -cpu host)
    else
        echo "warning: /dev/kvm not usable; falling back to emulation (very slow)" >&2
        ACCEL=(-accel tcg -cpu max)
    fi
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        [ "${ID:-}" = fedora ] || echo "note: written for a Fedora host; this is ${PRETTY_NAME:-?}" >&2
    fi
}

# ----------------------------------------------------------------- image
fetch_image() {
    if [ -n "$IMAGE" ]; then
        [ -f "$IMAGE" ] || die "no such image: $IMAGE"
        BASE=$IMAGE
        say "using $BASE (not verified)"
        return
    fi
    mkdir -p "$IMGDIR"
    local dir=$MIRROR/releases/$RELEASE/Cloud/$ARCH/images/ listing name sums
    say "looking up the Fedora $RELEASE Cloud image in $dir"
    listing=$(curl -fsSL --retry 3 "$dir") || die "cannot list $dir (try --mirror or --image)"
    name=$(grep -oE 'Fedora-Cloud-Base-Generic[^"<>/]*\.qcow2' <<< "$listing" | sort -uV | tail -n 1)
    sums=$(grep -oE 'Fedora-Cloud-[^"<>/]*CHECKSUM' <<< "$listing" | sort -uV | tail -n 1)
    if [ -z "$name" ] || [ -z "$sums" ]; then
        die "no Cloud Base Generic qcow2 or CHECKSUM listed in $dir"
    fi
    BASE=$IMGDIR/$name

    curl -fsSL --retry 3 -o "$IMGDIR/$sums" "$dir$sums"
    local key=/etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-$RELEASE-primary
    if command -v gpgv > /dev/null && command -v gpg > /dev/null && [ -f "$key" ]; then
        local ring
        ring=$(mktemp)
        gpg --dearmor < "$key" > "$ring"
        gpgv --keyring "$ring" "$IMGDIR/$sums" 2> /dev/null ||
            { rm -f "$ring"; die "signature check of $sums failed"; }
        rm -f "$ring"
        say "$sums: good signature from the Fedora $RELEASE key"
    else
        echo "warning: cannot check the CHECKSUM signature ($key or gpgv missing);" \
             "only the SHA-256 sum is checked" >&2
    fi
    local want have
    want=$(sed -n "s/^SHA256 ($name) = \([0-9a-f]*\)$/\1/p" "$IMGDIR/$sums")
    [ -n "$want" ] || die "$name not listed in $sums"
    if [ -f "$BASE" ] && [ "$(sha256sum "$BASE" | cut -d' ' -f1)" = "$want" ]; then
        say "$name already downloaded"
        return
    fi
    say "downloading $name"
    curl -fL --retry 3 -C - -o "$BASE.part" "$dir$name"
    have=$(sha256sum "$BASE.part" | cut -d' ' -f1)
    [ "$have" = "$want" ] || die "SHA-256 mismatch for $name"
    mv "$BASE.part" "$BASE"
    say "$name verified"
}

# -------------------------------------------------------------------- vm
make_vm() {
    mkdir -p "$VMDIR"
    [ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -C nmss-spike-host -f "$KEY"
    cat > "$VMDIR/user-data" <<EOF
#cloud-config
hostname: nmss-spike
users:
  - name: $VM_USER
    gecos: nm-sshuttle spike user
    groups: [wheel]
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    shell: /bin/bash
    lock_passwd: false
    plain_text_passwd: $VM_PASSWORD
    ssh_authorized_keys:
      - $(cat "$KEY.pub")
ssh_pwauth: false
EOF
    cat > "$VMDIR/meta-data" <<EOF
instance-id: nmss-spike-$(date +%s)
local-hostname: nmss-spike
EOF
    xorriso -as mkisofs -output "$VMDIR/seed.iso" -volid cidata -joliet -rock \
        "$VMDIR/user-data" "$VMDIR/meta-data" > "$VMDIR/xorriso.log" 2>&1 ||
        die "creating the cloud-init seed failed; see $VMDIR/xorriso.log"
    qemu-img create -q -f qcow2 -F qcow2 -b "$BASE" "$VMDIR/disk.qcow2" "${DISK_GB}G"
    cat > "$VMDIR/vm.env" <<EOF
GUI=$GUI
SSH_PORT=$SSH_PORT
VNC_DISPLAY=$VNC_DISPLAY
MEM=$MEM
CPUS=$CPUS
EOF
}

vm_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2> /dev/null; }

start_vm() {
    vm_running && { say "VM already running"; return; }
    if ss -ltnH "sport = :$SSH_PORT" 2> /dev/null | grep -q .; then
        die "port $SSH_PORT is in use (choose another with --ssh-port)"
    fi
    say "booting the VM (ssh on 127.0.0.1:$SSH_PORT, display on VNC 127.0.0.1:$((5900 + VNC_DISPLAY)))"
    qemu-system-$ARCH -name nmss-spike -machine q35 "${ACCEL[@]}" \
        -smp "$CPUS" -m "$MEM" \
        -drive "file=$VMDIR/disk.qcow2,if=virtio,discard=unmap" \
        -cdrom "$VMDIR/seed.iso" \
        -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22" \
        -device virtio-net-pci,netdev=net0 \
        -device virtio-rng-pci \
        -vga virtio -device qemu-xhci -device usb-tablet -audio none \
        -display none -vnc "127.0.0.1:$VNC_DISPLAY" \
        -serial "file:$VMDIR/console.log" \
        -daemonize -pidfile "$PIDFILE"
}

# The VM is recreated at will, so its host key is not pinned; the port only
# listens on 127.0.0.1.
vm_ssh() {
    ssh -i "$KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout=5 -o ServerAliveInterval=15 \
        "$VM_USER@127.0.0.1" "$@"
}

wait_ssh() {  # wait_ssh SECONDS
    for _ in $(seq 1 "$(( $1 / 5 ))"); do
        vm_ssh true 2> /dev/null && return 0
        vm_running || die "the VM exited; see $VMDIR/console.log"
        sleep 5
    done
    die "no SSH after $1 s; see $VMDIR/console.log"
}

wait_gnome() {
    for _ in $(seq 1 60); do
        vm_ssh "pgrep -u $VM_USER -x gnome-shell" > /dev/null 2>&1 && { sleep 5; return 0; }
        sleep 5
    done
    die "GNOME did not start for $VM_USER; try: $0 viewer"
}

push_spike() {
    tar -C "$REPO" --exclude=__pycache__ --exclude=spike-results -czf - spike lab/dns_server.py |
        vm_ssh "sudo rm -rf /opt/nm-sshuttle-spike && sudo mkdir -p /opt/nm-sshuttle-spike &&
                sudo tar -C /opt/nm-sshuttle-spike -xzf -"
}

reboot_vm() {
    say "rebooting the VM"
    vm_ssh "sudo systemctl reboot" > /dev/null 2>&1 || true
    sleep 15
    wait_ssh 600
}

provision() {
    if [ -f "$VMDIR/provisioned" ]; then
        say "VM already provisioned"
        return
    fi
    push_spike
    local args=(--user "$VM_USER")
    [ "$GUI" = 1 ] || args+=(--headless)
    [ -z "$SSHUTTLE_PIP" ] || args+=(--sshuttle-pip "$SSHUTTLE_PIP")
    say "provisioning (dnf upgrade$([ "$GUI" = 1 ] && echo ' + GNOME'); this takes a while)"
    vm_ssh "sudo bash /opt/nm-sshuttle-spike/spike/provision.sh ${args[*]}"
    reboot_vm
    touch "$VMDIR/provisioned"
}

open_viewer() {
    local url=vnc://127.0.0.1:$((5900 + VNC_DISPLAY))
    if command -v remote-viewer > /dev/null && { [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; }; then
        remote-viewer --title "nm-sshuttle spike VM" "$url" > /dev/null 2>&1 &
        say "opened the VM display ($url)"
    else
        say "open the VM display with any VNC viewer: $url (password for $VM_USER: $VM_PASSWORD)"
    fi
}

# --------------------------------------------------------------- commands
cmd_up() {
    check_host
    if [ ! -f "$VMDIR/disk.qcow2" ]; then
        fetch_image
        make_vm
    fi
    # shellcheck disable=SC1091
    . "$VMDIR/vm.env"
    start_vm
    say "waiting for the VM to boot and run cloud-init"
    wait_ssh 900
    provision
    if [ "$GUI" = 1 ]; then
        say "waiting for the GNOME session"
        wait_gnome
    fi
    say "VM ready"
}

cmd_run() {
    vm_running || die "VM not running (run: $0 up)"
    wait_ssh 60
    push_spike
    if [ "$GUI" = 1 ] && [ "$AUTO" = 0 ]; then
        wait_gnome
        open_viewer
        echo
        echo "The spike will ask you to look at and click things in the VM window."
        echo
    fi
    local args=(--user "$VM_USER" --report /var/tmp/nmss-spike-report) rc=0
    [ "$AUTO" = 1 ] && args+=(--auto)
    vm_ssh -t "sudo bash /opt/nm-sshuttle-spike/spike/spike.sh ${args[*]}" || rc=$?
    local dest
    dest=$REPO/spike-results/$(date +%Y%m%d-%H%M%S)
    mkdir -p "$dest"
    if vm_ssh "sudo tar -C /var/tmp -czf - nmss-spike-report" > "$dest/nmss-spike-report.tar.gz" &&
        tar -C "$dest" -xzf "$dest/nmss-spike-report.tar.gz"; then
        say "report: $dest/nmss-spike-report/report.md"
        say "to share all of it (raw logs included): $dest/nmss-spike-report.tar.gz"
    else
        echo "warning: could not copy the report back" >&2
    fi
    return "$rc"
}

cmd_down() {
    vm_running || { say "VM not running"; return; }
    say "shutting the VM down"
    vm_ssh "sudo systemctl poweroff" > /dev/null 2>&1 || true
    for _ in $(seq 1 60); do vm_running || return 0; sleep 1; done
    kill "$(cat "$PIDFILE")" 2> /dev/null || true
}

case $CMD in
    all) cmd_up; cmd_run ;;
    up) cmd_up ;;
    run) cmd_run ;;
    viewer) open_viewer ;;
    ssh) vm_ssh -t "${SSH_EXTRA[@]}" ;;
    status)
        if vm_running; then
            say "running (pid $(cat "$PIDFILE"), ssh 127.0.0.1:$SSH_PORT, VNC 127.0.0.1:$((5900 + VNC_DISPLAY)))"
        else
            say "not running"
        fi ;;
    down) cmd_down ;;
    destroy) cmd_down; rm -rf "$VMDIR"; say "deleted $VMDIR (images kept in $IMGDIR)" ;;
esac
