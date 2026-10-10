#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Run the M2 tests (test-vm/run.sh) in a throwaway Fedora 44 VM, from a Fedora
# 44 host. The VM handling (image download and check, QEMU, cloud-init, SSH,
# provisioning) is spike/vm.sh's; this script calls it with its own state
# directory and port, so it does not disturb a spike VM. It adds what the real
# plugin needs: meson and firewalld in the VM, the repository copied in,
# built and installed with meson, and test-vm/run.sh run as root.
#
#   test-vm/vm.sh [COMMAND] [OPTIONS]
#
# Commands:
#   all       (default) up + run
#   up        create, boot and provision the VM (headless), install meson, firewalld
#   run       copy the repository in, install it, run the tests, copy the report back
#   ssh       open a shell in the VM (or run: vm.sh ssh -- CMD...)
#   viewer, status, down, destroy   as in spike/vm.sh
#
# Options:
#   --only IDS            run only these scenarios, e.g. M2-05,M2-06
#   --suspend             also run M2-28 (suspend; the VM must wake from its RTC)
#   --gnome               provision GNOME too (the tests do not use it)
#   --workdir DIR         state directory (default ~/.cache/nm-sshuttle-test-vm)
#   --ssh-port PORT       host port forwarded to the VM's SSH (default 2245)
#   --vnc-display N       VNC display on 127.0.0.1 (default 45)
#   --mem MB, --cpus N, --disk GB, --image FILE, --mirror URL, --sshuttle-pip VER
#                         passed to spike/vm.sh
#
# Report: test-vm-results/<timestamp>/ (report.md, details.md, raw/)
set -uo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
SPIKE_VM=$REPO/spike/vm.sh
VM_USER=tester
DEST=/opt/nm-sshuttle-test
REPORT_DIR=/var/tmp/nm-sshuttle-test-report

CMD=all
ONLY=""
SUSPEND=""
GUI_ARGS=(--headless)
WORKDIR=${XDG_CACHE_HOME:-$HOME/.cache}/nm-sshuttle-test-vm
SSH_PORT=2245
VNC_DISPLAY=45
FWD=()
SSH_EXTRA=()

case ${1:-} in
    all|up|run|viewer|ssh|status|down|destroy) CMD=$1; shift ;;
esac
while [ $# -gt 0 ]; do
    case $1 in
        --only) ONLY=$2; shift 2 ;;
        --suspend) SUSPEND=--suspend; shift ;;
        --gnome) GUI_ARGS=(); shift ;;
        --workdir) WORKDIR=$2; shift 2 ;;
        --ssh-port) SSH_PORT=$2; shift 2 ;;
        --vnc-display) VNC_DISPLAY=$2; shift 2 ;;
        --mem|--cpus|--disk|--image|--mirror|--sshuttle-pip) FWD+=("$1" "$2"); shift 2 ;;
        --) shift; SSH_EXTRA=("$@"); break ;;
        -h|--help) sed -n '3,29p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
    esac
done

COMMON=(--workdir "$WORKDIR" --ssh-port "$SSH_PORT" --vnc-display "$VNC_DISPLAY")

say() { echo "==> $*"; }
die() { echo "error: $*" >&2; exit 1; }
[ -x "$SPIKE_VM" ] || die "$SPIKE_VM is missing"

# One command in the VM, through spike/vm.sh's SSH settings (key, port, no host key pinning)
vm() { "$SPIKE_VM" ssh "${COMMON[@]}" -- "$*"; }

prepare() {
    if vm "test -e /var/lib/nmtest-prepared" < /dev/null; then
        return
    fi
    say "installing meson, ninja and firewalld in the VM"
    vm "sudo dnf -y install meson ninja-build firewalld nftables iproute python3-gobject-base sshuttle &&
        sudo systemctl enable --now firewalld &&
        sudo loginctl enable-linger $VM_USER &&
        sudo touch /var/lib/nmtest-prepared" < /dev/null || die "preparing the VM failed"
}

cmd_up() {
    "$SPIKE_VM" up "${COMMON[@]}" "${GUI_ARGS[@]}" "${FWD[@]}" || die "spike/vm.sh up failed"
    prepare
}

cmd_run() {
    local dest rc=0 args
    "$SPIKE_VM" status "${COMMON[@]}" | grep -q '^==> running' || die "VM not running (run: $0 up)"
    prepare
    say "copying the repository to $DEST"
    tar -C "$REPO" --exclude=.git --exclude=build --exclude=spike-results \
        --exclude=test-vm-results --exclude=__pycache__ --exclude=.pytest_cache -czf - . |
        vm "sudo rm -rf $DEST && sudo mkdir -p $DEST && sudo tar -C $DEST -xzf -" ||
        die "copying the repository failed"
    say "meson setup, install, nm-sshuttle post-install"
    vm "sudo bash $DEST/test-vm/install.sh" || die "installing the plugin failed"
    args="--user $VM_USER --report $REPORT_DIR"
    [ -z "$ONLY" ] || args="$args --only $ONLY"
    [ -z "$SUSPEND" ] || args="$args $SUSPEND"
    say "running the tests (this takes a while: some scenarios wait for timers of 60 s and more)"
    vm "sudo bash $DEST/test-vm/run.sh $args" || rc=$?
    dest=$REPO/test-vm-results/$(date +%Y%m%d-%H%M%S)
    mkdir -p "$dest"
    if vm "sudo tar -C /var/tmp -czf - nm-sshuttle-test-report" < /dev/null > "$dest/report.tar.gz" &&
        tar -C "$dest" -xzf "$dest/report.tar.gz"; then
        say "report: $dest/nm-sshuttle-test-report/report.md"
        say "FAIL details: $dest/nm-sshuttle-test-report/details.md"
        say "to share all of it: $dest/report.tar.gz"
    else
        echo "warning: could not copy the report back" >&2
    fi
    return "$rc"
}

case $CMD in
    all) cmd_up && cmd_run ;;
    up) cmd_up ;;
    run) cmd_run ;;
    ssh) "$SPIKE_VM" ssh "${COMMON[@]}" ${SSH_EXTRA:+--} "${SSH_EXTRA[@]}" ;;
    viewer|status|down|destroy) "$SPIKE_VM" "$CMD" "${COMMON[@]}" ;;
esac
