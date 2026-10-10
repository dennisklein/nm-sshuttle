#!/bin/bash
# SPDX-License-Identifier: MIT
#
# nm-sshuttle M1 spike: answers the risks listed in docs/design.md §6 on a
# real Fedora 44 system with NetworkManager, systemd-resolved, SELinux and
# (optionally) GNOME. Run as root inside the throwaway VM that spike/vm.sh
# creates; vm.sh calls it as:
#
#   sudo spike/spike.sh --user tester [--auto] [--report DIR]
#
# It installs a bare VPN plugin (spike/plugin/), builds a self-contained
# "jump host + internal network" topology with network namespaces, drives
# NetworkManager through the scenarios below, writes report.md plus raw
# diagnostics to the report directory, and undoes everything at the end
# (unless --keep). "spike.sh cleanup" undoes a kept or aborted run.
#
#   R0  ssh as the session user through the bridge, key from the user's agent
#   R1  plugin activation: spawned directly by NM (with a plain and with a
#       NetworkManager-* named tunnel unit), and via shim + D-Bus activation
#   R2  SELinux domains and denials for each
#   R3  per-tunnel link: NM addressing, resolved split DNS, lookups through the
#       tunnel; also dns=none with a link
#   R4  reconnect modes: identical, nbns sentinel (also without firewalld),
#       two-phase sentinel, perturb, invisible, relink-wait last (it crashed
#       NetworkManager 1.56.1 without the wait); after each recovery NM's
#       DNS entries and a name lookup; switching off during a reconnect;
#       giving up from "connecting" in three orders
#   R5  GNOME: Quick Settings toggle; Settings with and without an auth dialog
#       (manual, needs GNOME)
#   R6  gcr-ssh-agent unlock prompt for a locked key, and a reconnect behind
#       the lock screen with the key unloaded (manual, needs GNOME)
#   R7  NetworkManager restarting under an active VPN
set -uo pipefail
# No pager anywhere. From systemd 259's source, its tools fork less on a tty,
# and also with stdout and stderr on /dev/null (terminal-util.c). Run 5's R4h-p
# FAIL came from a 'busctl status' on the tty; the pager is suspected, not
# proven. pager.c returns early for "cat".
export SYSTEMD_PAGER=cat PAGER=cat

SRC=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$SRC")
LIBEXEC=/usr/local/libexec/nm-sshuttle-spike
STATE=/run/nm-sshuttle-spike
VARDIR=/var/lib/nmss-spike
# /usr/lib/NetworkManager/VPN is what NetworkManager creates; /etc/NetworkManager/VPN
# is deprecated and does not exist on Fedora.
NAME_FILE=/usr/lib/NetworkManager/VPN/nm-sshuttle-spike.name
NM_CONF=/etc/NetworkManager/conf.d/99-nmss-spike.conf
AUDIT_LOG=/var/log/audit/audit.log
DBUS_POLICY=/etc/dbus-1/system.d/nm-sshuttle-spike.conf
DBUS_SERVICE=/usr/share/dbus-1/system-services/org.freedesktop.NetworkManager.sshuttle.service
PLUGIN_UNIT=nm-sshuttle-spike.service
TUNNEL_UNIT=nm-sshuttle-spike-tunnel.service
# Same unit under a name Fedora's policy labels NetworkManager_unit_file_t,
# which NetworkManager_t may start and stop (docs/design.md §6, R1e).
NM_TUNNEL_UNIT=NetworkManager-sshuttle-spike-tunnel.service
PROFILE_EXTRA=""   # extra vpn.data items for one phase
NM_UNMANAGED_CONF=/usr/lib/NetworkManager/conf.d/90-nm-sshuttle-spike.conf
UPLINK=""          # the uplink device, found in preflight
NM_DNS_PATH=org.freedesktop.NetworkManager
NM_DNS_OBJ=/org/freedesktop/NetworkManager/DnsManager
NM_DNS_IFACE=org.freedesktop.NetworkManager.DnsManager
# What R1 found to work; every phase after R1 uses these.
PLUGIN_MODE=shim
UNIT=$TUNNEL_UNIT
SSHD_DIR=/etc/ssh/nmss-spike
BUS=org.freedesktop.NetworkManager.sshuttle
CON=nmss-spike
JUMP_IP=198.51.100.1
HOST_IP=198.51.100.2
WEB_IP=10.99.0.10
DNS_IP=10.99.0.53
RECONNECT_DELAY=20
LOCKED_PASSPHRASE=spike-locked

USER_NAME=${SUDO_USER:-}
AUTO=0
KEEP=0
REPORT=/var/tmp/nmss-spike-report
CMD=run

while [ $# -gt 0 ]; do
    case $1 in
        --user) USER_NAME=$2; shift 2 ;;
        --auto) AUTO=1; shift ;;
        --keep) KEEP=1; shift ;;
        --report) REPORT=$2; shift 2 ;;
        cleanup) CMD=cleanup; shift ;;
        -h|--help) sed -n '3,25p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 2; }
[ -n "$USER_NAME" ] || { echo "--user is required" >&2; exit 2; }
UID_U=$(id -u "$USER_NAME") || exit 2
GID_U=$(id -g "$USER_NAME")
HOME_U=$(getent passwd "$USER_NAME" | cut -d: -f6)
RT_U=/run/user/$UID_U
AGENT_SOCK=""

# ------------------------------------------------------------------ helpers
RESULTS=()
result() {
    local id=$1 st=$2
    shift 2
    RESULTS+=("$id|$st|$*")
    printf '%-4s %-5s %s\n' "$st" "$id" "$*"
}
passed() {  # passed ID : did check ID pass?
    printf '%s\n' "${RESULTS[@]}" | grep -q "^$1|PASS|"
}
# check ID TEXT CMD... : PASS or FAIL by CMD's exit code. Each check's start
# time goes to raw/check-times.txt; a failing command's output and exit code
# go to raw/check-fail.txt, captured on the first and only run (some checks
# have side effects, such as a DNS reload).
check() { check_unless_trivial "$1" "$2" "" "${@:3}"; }
# check_unless_trivial ID TEXT WHY CMD... : like check, but a non-empty WHY
# says why a PASS would be trivial, and turns it into INFO. A FAIL stays a FAIL.
check_unless_trivial() {
    local id=$1 text=$2 why=$3 out=$VARDIR/check-out rc
    shift 3
    echo "$(date +%T.%N) $id" >> "$REPORT/raw/check-times.txt"
    "$@" > "$out" 2>&1
    rc=$?
    if [ "$rc" = 0 ] && [ -n "$why" ]; then
        result "$id" INFO "passes only trivially ($why): $text"
    elif [ "$rc" = 0 ]; then
        result "$id" PASS "$text"
    else
        result "$id" FAIL "$text"
        { echo "### $(date +%T.%N) $id: $text"; printf '$'; printf ' %q' "$@"; echo
          cat "$out"; echo "exit=$rc"; echo; } >> "$REPORT/raw/check-fail.txt"
    fi
    rm -f "$out"
}
note() { echo; echo "== $*"; }
die() { echo "spike: $*" >&2; exit 1; }
write_file() {  # write_file PATH < content : create parent dirs, fail loudly
    if ! { mkdir -p "$(dirname "$1")" && cat > "$1" && chmod 644 "$1"; }; then
        die "cannot write $1"
    fi
}
# detail ID TITLE : append stdin as an excerpt to the report's Details section
detail() {
    { echo "### $1: $2"; echo; echo '```'; tail -n 60; echo '```'; echo; } >> "$REPORT/details.md"
}
nm_journal() {  # nm_journal EPOCH : NM, plugin and tunnel journal lines about the spike
    journalctl --since "@$1" --no-pager -o short-precise \
        -u NetworkManager -u "$PLUGIN_UNIT" -u "$TUNNEL_UNIT" -u "$NM_TUNNEL_UNIT" 2>&1 |
        grep -i -E 'vpn|sshuttle|nmss|nm-sshuttle|error|fail|denied|assert|dumped' | tail -n 60
}
raw() {  # raw FILE CMD... : append command and its output to raw/FILE.txt
    local f=$REPORT/raw/$1.txt
    shift
    { echo "\$ $*"; "$@" 2>&1; echo; } >> "$f"
}
ask() {  # ask PROMPT -> answer on stdout ("" in --auto mode)
    local ans=""
    if [ "$AUTO" = 0 ] && [ -r /dev/tty ]; then
        read -r -p "$1 " ans < /dev/tty
    fi
    printf '%s' "$ans"
}
yes_answer() { case $1 in y|Y|yes|Yes) return 0 ;; *) return 1 ;; esac; }
contains() { case $1 in *"$2"*) return 0 ;; *) return 1 ;; esac; }

# Run a command as the session user, connected to the user's own manager.
as_user() {
    setpriv --reuid="$UID_U" --regid="$GID_U" --init-groups --reset-env \
        env HOME="$HOME_U" USER="$USER_NAME" LOGNAME="$USER_NAME" \
            PATH=/usr/local/bin:/usr/bin:/bin XDG_RUNTIME_DIR="$RT_U" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=$RT_U/bus" \
            ${AGENT_SOCK:+SSH_AUTH_SOCK=$AGENT_SOCK} "$@"
}
user_manager_env() { as_user systemctl --user show-environment 2>/dev/null | sed -n "s/^$1=//p"; }

con_state() { nmcli -g GENERAL.STATE connection show "$CON" 2>/dev/null | head -n 1; }
wait_state() {  # wait_state STATE SECONDS  ("gone" = not active)
    local i s
    for i in $(seq 1 "$(( $2 * 2 ))"); do
        s=$(con_state)
        [ "${s:-gone}" = "$1" ] && return 0
        sleep 0.5
    done
    return 1
}
http_ok() { [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' --noproxy '*' "$1")" = 200 ]; }
con_gone() { [ -z "$(con_state)" ]; }
# NM caches DnsManager.Configuration until it next pushes changed DNS content;
# "reload dns-rc" makes it rebuild the property (design §2.1). Read resolved
# first where both matter: the reload also pushes DNS again.
nm_dns_config() {
    nmcli general reload dns-rc > /dev/null 2>&1
    sleep 1
    busctl --system get-property "$NM_DNS_PATH" "$NM_DNS_OBJ" "$NM_DNS_IFACE" Configuration
}
nm_dns_json() {
    nmcli general reload dns-rc > /dev/null 2>&1
    sleep 1
    busctl --system --json=short get-property "$NM_DNS_PATH" "$NM_DNS_OBJ" "$NM_DNS_IFACE" Configuration
}
nmss0_no_default_route() { [ "$(resolvectl default-route nmss0 2> /dev/null | awk '{print $NF}')" = no ]; }
uplink_has_dns() { resolvectl dns "$UPLINK" 2> /dev/null | sed 's/^Link [0-9]* ([^)]*)://' | grep -q '[0-9]'; }
resolved_split_ok() { nmss0_no_default_route && uplink_has_dns; }
uplink_restored() {
    uplink_has_dns && ip -4 route show default dev "$UPLINK" | grep -q . &&
        [ "$(resolvectl default-route "$UPLINK" 2> /dev/null | awk '{print $NF}')" = yes ]
}
# non-VPN DNS entries NM keeps for nmss0 (its external device's, design §4.5)
nmss0_device_entries() {
    nm_dns_json | python3 -I -c '
import json, sys
print(sum(1 for e in json.load(sys.stdin)["data"]
          if not e.get("vpn", {}).get("data") and e.get("interface", {}).get("data") == "nmss0"))'
}
no_nmss0_device_entry() { [ "$(nmss0_device_entries)" = 0 ]; }
no_wins_sentinel() { ! nmcli -g IP4.WINS connection show "$CON" | grep -q 192.0.0.10; }
link_has_dns() { resolvectl dns nmss0 | grep -q "$DNS_IP"; }
vpn_dns_entries() {  # number of VPN entries in NM's DNS configuration
    nm_dns_json |
        python3 -I -c 'import json, sys; print(sum(1 for e in json.load(sys.stdin)["data"] if e.get("vpn", {}).get("data")))'
}
resolved_dropped() { ! resolvectl dns | grep -q "$DNS_IP"; }
no_config_after_disconnect() { ! sed -n '/disconnect requested/,$p' "$1" | grep -q Config; }
# NM's DnsManager after a (re)configuration: exactly one VPN entry, on nmss0,
# and no entry without an interface (design §4.4 checklist).
vpn_dns_registered_once() {
    nm_dns_json | python3 -I -c '
import json, sys
entries = json.load(sys.stdin)["data"]
ours = [e for e in entries if e.get("vpn", {}).get("data")
        and sys.argv[1] in e.get("nameservers", {}).get("data", [])]
bare = [e for e in entries if "interface" not in e]
ok = len(ours) == 1 and ours[0].get("interface", {}).get("data") == "nmss0" and not bare
sys.exit(0 if ok else 1)' "$DNS_IP"
}
# Does NM's rebuilt configuration have a VPN entry for $DNS_IP on nmss0? An
# entry for a dead ifindex has no interface after the rebuild.
vpn_entry_on_nmss0() {
    nm_dns_json | python3 -I -c '
import json, sys
sys.exit(0 if any(e.get("vpn", {}).get("data") and e.get("interface", {}).get("data") == "nmss0"
                  and sys.argv[1] in e.get("nameservers", {}).get("data", [])
                  for e in json.load(sys.stdin)["data"]) else 1)' "$DNS_IP"
}
nmss0_ifindex() { cat /sys/class/net/nmss0/ifindex 2> /dev/null; }
# relink_why IFINDEX : why a risk-11 PASS would be trivial after nmss0 was
# replaced; empty while nmss0 keeps IFINDEX.
relink_why() {
    local ifi
    ifi=$(nmss0_ifindex)
    if [ -z "$1" ] || [ "$ifi" != "$1" ]; then
        echo "nmss0 is ifindex ${ifi:-none} now, but NM filed the VPN's DNS under ${1:-none} (relink)"
    fi
}
# entry_why WHY : WHY, or why -u would be trivial without a VPN entry on nmss0.
entry_why() {
    if [ -n "$1" ]; then echo "$1"
    elif ! vpn_entry_on_nmss0; then echo "NM's VPN DNS entry is not on nmss0 (see -n)"
    fi
}
split_name_resolves() { contains "$(resolvectl query --cache=no git.corp.test 2>&1)" "$WEB_IP"; }
# In an NM trace that starts before a drop: did NM reach "activated" only after
# the plugin's new Config? An early flip (design §4.4) activates before it.
activated_after_config() {
    awk '/dbus: state changed: starting \(3\)/ && !d {d = 1; next}
        d && /config: reply received/ && !c {c = NR}
        d && /set state: activated \(was pre-up\)/ && !a {a = NR}
        END {exit !(c && a && a > c)}' "$1"
}
# Did NM ever add the sentinel (WINS 192.0.0.10) l3cd to l3cfg?
sentinel_committed() {
    awk '/l3cd\[ip-4\]: set / {cur = $NF}
        /wins\[0\]: 192\.0\.0\.10/ {sent[cur] = 1}
        /l3cd\[ip-4\]: add-config / && sent[$NF] {hit = 1}
        END {print hit ? "yes" : "no"}' "$1"
}
tunnel_cleaned() {
    ! systemctl is-active --quiet "$UNIT" && [ ! -e /sys/class/net/nmss0 ] && [ -z "$(sshuttle_tables)" ]
}
sshuttle_tables() { nft list tables 2>/dev/null | awk '$3 ~ /^sshuttle-ipv[46]-[0-9]+$/ {print $3}'; }
plugin_field() { busctl --system --no-pager status "$BUS" 2>/dev/null | sed -n "s/^$1=//p" | head -n 1; }
dbus_call() { busctl --system --no-pager call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus "$@"; }
# Is the plugin's bus name owned? Fails with NameHasNoOwner otherwise.
bus_owned() { dbus_call GetNameOwner s "$BUS" > /dev/null 2>&1; }
# PID of the process that owns the plugin's bus name, never from 'busctl
# status' (run 5's R4h-p). busctl prints 's ":1.N"' and 'u PID'.
bus_owner_pid() {
    local o p
    o=$(dbus_call GetNameOwner s "$BUS" 2> /dev/null) || return 1
    o=${o#s \"}
    o=${o%\"}
    p=$(dbus_call GetConnectionUnixProcessID s "$o" 2> /dev/null) || return 1
    echo "${p#u }"
}
# The plugin's PID for signals: the bus name's owner, else its own pid file.
plugin_pid() { bus_owner_pid || cat "$STATE/plugin.pid" 2> /dev/null; }
# plugin_owns_bus PID : the process is alive and owns the plugin's bus name.
plugin_owns_bus() {
    kill -0 "$1" 2> /dev/null && [ "$(bus_owner_pid)" = "$1" ]
}
dbus_reload() {
    dbus_call ReloadConfig > /dev/null 2>&1 || systemctl reload dbus-broker 2>/dev/null
}
audit_offset() { stat -c %s "$AUDIT_LOG" 2> /dev/null || echo -1; }
# avc_count_since EPOCH OFFSET NAME -> number of SELinux denial records since
# then; raw records in raw/avc-NAME.txt, a summary in the report's details.
avc_count_since() {
    local ts=$1 off=$2 f=$REPORT/raw/avc-$3.txt
    if [ "$off" -ge 0 ] && [ -r "$AUDIT_LOG" ]; then
        tail -c +"$((off + 1))" "$AUDIT_LOG" | grep -E '^type=(AVC|USER_AVC|SELINUX_ERR)' > "$f"
    else
        # auditd not logging: the kernel and dbus-broker report to the journal
        journalctl --since "@$ts" --no-pager -o cat 2> /dev/null | grep -E 'avc: +denied|SELINUX_ERR' > "$f"
    fi
    local n
    n=$(wc -l < "$f")
    if [ "$n" -gt 0 ]; then
        sed -E 's/^(type=[A-Z_]+) msg=audit\([^)]*\): pid=[0-9]+ uid=[0-9]+ auid=[0-9]+ ses=[0-9]+/\1/' "$f" |
            sort | uniq -c | sort -rn | detail "R2-$3" "SELinux denials ($n records, deduplicated)"
    fi
    echo "$n"
}

# --------------------------------------------------------------- teardown
deactivate() {
    echo "$(date +%T.%N) deactivate" >> "$REPORT/raw/check-times.txt"
    nmcli connection down "$CON" > /dev/null 2>&1
    wait_state gone 30
    local i
    for i in $(seq 1 30); do
        bus_owned || return 0
        sleep 1
    done
    systemctl stop "$PLUGIN_UNIT" 2>/dev/null
    [ -f "$STATE/plugin.pid" ] && kill "$(cat "$STATE/plugin.pid")" 2>/dev/null
    sleep 1
}

cleanup() {
    set +e
    note "cleaning up"
    nmcli connection down "$CON" > /dev/null 2>&1
    nmcli connection delete "$CON" > /dev/null 2>&1
    systemctl stop "$TUNNEL_UNIT" "$NM_TUNNEL_UNIT" "$PLUGIN_UNIT" 2>/dev/null
    systemctl stop nmss-spike-sshd nmss-spike-dns nmss-spike-web 2>/dev/null
    [ -f "$STATE/plugin.pid" ] && kill "$(cat "$STATE/plugin.pid")" 2>/dev/null
    rm -f "$NAME_FILE" /etc/NetworkManager/VPN/nm-sshuttle-spike.name "$NM_CONF" \
        "$DBUS_POLICY" "$DBUS_SERVICE" \
        "/etc/systemd/system/$PLUGIN_UNIT" "/etc/systemd/system/$TUNNEL_UNIT" \
        "/etc/systemd/system/$NM_TUNNEL_UNIT"
    nmcli general reload conf > /dev/null 2>&1
    systemctl daemon-reload
    dbus_reload
    if [ -f "$VARDIR/nm-logging" ]; then
        # terse output escapes per-domain levels as DOMAIN\:LEVEL
        # shellcheck disable=SC2046
        nmcli general logging level $(cut -d: -f1 "$VARDIR/nm-logging") \
            domains "$(cut -d: -f2- "$VARDIR/nm-logging" | sed 's/\\:/:/g')" 2> /dev/null
    fi
    [ -f "$VARDIR/firewalld-stopped" ] && systemctl start firewalld
    if [ -f "$NM_UNMANAGED_CONF" ]; then
        rm -f "$NM_UNMANAGED_CONF"
        systemctl restart NetworkManager
    fi
    [ -s "$VARDIR/uplink-modified" ] && nmcli device reapply "$(cat "$VARDIR/uplink-modified")"
    for t in $(sshuttle_tables); do nft delete table inet "$t"; done
    ip link del h-j 2>/dev/null
    ip link del nmss0 2>/dev/null
    for n in jump internal nmss-void; do ip netns del "$n" 2>/dev/null; done
    if [ -d "$HOME_U/.ssh" ]; then
        # Remove only the spike's own keys from the agent, never the user's.
        AGENT_SOCK=${AGENT_SOCK:-$(user_manager_env SSH_AUTH_SOCK)}
        local k
        for k in "$HOME_U"/.ssh/nmss_spike_*.pub; do
            [ -e "$k" ] && as_user ssh-add -d "$k" > /dev/null 2>&1
        done
        rm -f "$HOME_U"/.ssh/nmss_spike_*
        as_user ssh-keygen -R "$JUMP_IP" > /dev/null 2>&1
    fi
    if [ -f "$VARDIR/started-agent" ]; then
        as_user systemctl --user stop nmss-spike-agent 2>/dev/null
        as_user systemctl --user unset-environment SSH_AUTH_SOCK 2>/dev/null
    fi
    if [ -f "$VARDIR/enabled-gcr-socket" ]; then
        as_user systemctl --user disable --now gcr-ssh-agent.socket 2>/dev/null
    fi
    rm -rf "$LIBEXEC" "$SSHD_DIR" "$VARDIR" "$STATE"
    echo "done"
}

if [ "$CMD" = cleanup ]; then
    cleanup
    exit 0
fi

# ---------------------------------------------------------------- setup
preflight() {
    note "preflight"
    rm -rf "$REPORT"
    mkdir -p "$REPORT/raw" "$VARDIR"
    START_EPOCH=$(date +%s)
    START_OFFSET=$(audit_offset)
    # shellcheck disable=SC1091
    . /etc/os-release
    [ "${ID:-}" = fedora ] || echo "warning: not Fedora (${PRETTY_NAME:-unknown})"
    [ "${VERSION_ID:-}" = 44 ] || echo "warning: written for Fedora 44, this is ${PRETTY_NAME:-unknown}"
    local missing=""
    for c in nmcli nft ip ss setpriv /usr/sbin/sshd ssh ssh-keygen ssh-keyscan ssh-add busctl \
             resolvectl curl python3 sshuttle systemd-run journalctl; do
        command -v "$c" > /dev/null || missing="$missing $c"
    done
    [ -z "$missing" ] || { echo "missing:$missing (run spike/provision.sh first)" >&2; exit 2; }
    [ -d "$RT_U" ] || { echo "$USER_NAME has no session (no $RT_U); log in first" >&2; exit 2; }

    GUI=0
    pgrep -u "$USER_NAME" -x gnome-shell > /dev/null && GUI=1
    {
        echo "os: ${PRETTY_NAME:-?}"
        echo "kernel: $(uname -r)"
        echo "NetworkManager: $(NetworkManager --version 2>/dev/null)"
        echo "systemd: $(systemctl --version | head -n 1)"
        echo "sshuttle: $(sshuttle --version 2>&1) ($(command -v sshuttle))"
        echo "gnome-shell: $(gnome-shell --version 2>/dev/null || echo none)"
        echo "selinux: $(getenforce 2>/dev/null || echo n/a)"
        echo "resolved: $(systemctl is-active systemd-resolved) ($(readlink /etc/resolv.conf))"
        echo "firewalld: $(systemctl is-active firewalld 2>/dev/null)"
        echo "auditd: $(systemctl is-active auditd 2>/dev/null) ($( [ -r "$AUDIT_LOG" ] && echo "$AUDIT_LOG" || echo "no $AUDIT_LOG"))"
        local d
        for d in /usr/lib/NetworkManager/VPN /etc/NetworkManager/VPN; do
            echo "$d: $([ -d "$d" ] && echo exists || echo missing)"
        done
        echo "gnome session for $USER_NAME: $([ "$GUI" = 1 ] && echo yes || echo no)"
        echo "selinux-policy: $(rpm -q selinux-policy-targeted 2>/dev/null || echo ?)"
        echo "audit: $(auditctl -s 2>/dev/null | grep -E '^(enabled|lost|backlog) ' | xargs)"
    } | tee "$REPORT/system.txt"
    UPLINK=$(ip -4 route show default | awk '{print $5; exit}')
    echo "uplink: ${UPLINK:-none}" | tee -a "$REPORT/system.txt"
    # Start from a fresh NetworkManager: run 3's had been running for 36 h, since
    # run 2's crash.
    systemctl restart NetworkManager
    nm-online -s -q -t 60
    echo "NetworkManager restarted for this run (pid $(systemctl show -p MainPID --value NetworkManager))" |
        tee -a "$REPORT/system.txt"
    raw system loginctl list-sessions --no-legend
    raw system nmcli general status
    raw system nmcli device status
    # shellcheck disable=SC2016
    raw system sh -c 'echo "TERM=${TERM:-}"; stty size; command -v less'
}

agent_setup() {
    note "user agent"
    AGENT_SOCK=$(user_manager_env SSH_AUTH_SOCK)
    if [ -z "$AGENT_SOCK" ] && [ "$GUI" = 1 ] &&
        as_user systemctl --user cat gcr-ssh-agent.socket > /dev/null 2>&1; then
        result R0a INFO "SSH_AUTH_SOCK missing from the user manager; enabling gcr-ssh-agent.socket"
        as_user systemctl --user enable --now gcr-ssh-agent.socket > /dev/null 2>&1 &&
            touch "$VARDIR/enabled-gcr-socket"
        sleep 1
        AGENT_SOCK=$(user_manager_env SSH_AUTH_SOCK)
    fi
    if [ -z "$AGENT_SOCK" ]; then
        AGENT_SOCK=$RT_U/nmss-spike-agent.sock
        as_user systemd-run --user --quiet --collect --unit=nmss-spike-agent \
            ssh-agent -D -a "$AGENT_SOCK" > /dev/null
        as_user systemctl --user set-environment SSH_AUTH_SOCK="$AGENT_SOCK"
        touch "$VARDIR/started-agent"
        sleep 1
    fi
    case $AGENT_SOCK in
        */gcr/ssh) AGENT_KIND=gcr ;;
        */keyring/ssh) AGENT_KIND=gnome-keyring ;;
        */nmss-spike-agent.sock) AGENT_KIND=spike-ssh-agent ;;
        *) AGENT_KIND=other ;;
    esac
    result R0b INFO "agent: $AGENT_KIND ($AGENT_SOCK)"

    install -d -m 700 -o "$USER_NAME" -g "$GID_U" "$HOME_U/.ssh"
    rm -f "$HOME_U"/.ssh/nmss_spike_auto*
    as_user ssh-keygen -q -t ed25519 -N '' -C nmss-spike-auto -f "$HOME_U/.ssh/nmss_spike_auto"
    as_user ssh-add -q "$HOME_U/.ssh/nmss_spike_auto" < /dev/null
    raw agent as_user ssh-add -l
}

topology() {
    note "topology: host -> jump ($JUMP_IP, sshd) -> internal (DNS $DNS_IP, web $WEB_IP)"
    set -e
    ip netns add jump
    ip netns add internal
    # '+=' adds to, and so keeps, any unmanaged-devices list in /usr/lib (R8)
    printf '[keyfile]\nunmanaged-devices+=interface-name:h-j\n' | write_file "$NM_CONF"
    nmcli general reload conf 2> /dev/null || true
    ip link add h-j type veth peer name j-h
    nmcli device set h-j managed no 2> /dev/null || true
    ip link set j-h netns jump
    ip addr add $HOST_IP/24 dev h-j
    ip link set h-j up
    ip -n jump addr add $JUMP_IP/24 dev j-h
    ip -n jump link set j-h up
    ip -n jump link set lo up
    ip -n jump link add j-i type veth peer name i-j
    ip -n jump link set i-j netns internal
    ip -n jump addr add 10.99.0.1/24 dev j-i
    ip -n jump link set j-i up
    ip -n internal addr add $WEB_IP/24 dev i-j
    ip -n internal addr add $DNS_IP/24 dev i-j
    ip -n internal link set i-j up
    ip -n internal link set lo up
    ip -n internal route add default via 10.99.0.1
    ip netns exec jump sysctl -qw net.ipv4.ip_forward=1

    install -d -m 755 "$SSHD_DIR" "$SSHD_DIR/authorized_keys" "$VARDIR/www"
    ssh-keygen -q -t ed25519 -N '' -f "$SSHD_DIR/host_ed25519"
    write_file "$SSHD_DIR/sshd_config" <<EOF
ListenAddress $JUMP_IP
HostKey $SSHD_DIR/host_ed25519
AuthorizedKeysFile $SSHD_DIR/authorized_keys/%u
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
UsePAM no
PidFile none
EOF
    install -m 644 "$HOME_U/.ssh/nmss_spike_auto.pub" "$SSHD_DIR/authorized_keys/$USER_NAME"
    echo "nm-sshuttle spike: reached through the tunnel" > "$VARDIR/www/index.html"
    restorecon -RF "$SSHD_DIR" "$VARDIR" 2> /dev/null || true

    systemd-run --quiet --collect --unit=nmss-spike-sshd \
        -p NetworkNamespacePath=/run/netns/jump /usr/sbin/sshd -D -e -f "$SSHD_DIR/sshd_config"
    systemd-run --quiet --collect --unit=nmss-spike-dns \
        -p NetworkNamespacePath=/run/netns/internal /usr/bin/python3 "$REPO/lab/dns_server.py" $DNS_IP
    systemd-run --quiet --collect --unit=nmss-spike-web \
        -p NetworkNamespacePath=/run/netns/internal \
        /usr/bin/python3 -m http.server --bind $WEB_IP 8080 --directory "$VARDIR/www"
    set +e
    local i
    for i in $(seq 1 50); do
        ss -N jump -ltnH 'sport = :22' | grep -q . &&
            ss -N internal -ltnH 'sport = :8080' | grep -q . &&
            ss -N internal -lunH 'sport = :53' | grep -q . && break
        sleep 0.2
    done
    rm -f "$VARDIR/known_hosts.tmp"
    ssh-keyscan -T 5 -t ed25519 $JUMP_IP > "$VARDIR/known_hosts.tmp" 2> /dev/null
    as_user ssh-keygen -R $JUMP_IP > /dev/null 2>&1
    cat "$VARDIR/known_hosts.tmp" >> "$HOME_U/.ssh/known_hosts"
    chown "$USER_NAME:" "$HOME_U/.ssh/known_hosts"
    raw topology systemctl status nmss-spike-sshd nmss-spike-dns nmss-spike-web --no-pager
}

install_plugin() {
    note "installing the spike plugin"
    install -d "$LIBEXEC"
    find "$SRC/plugin" -maxdepth 1 -type f -exec install -m 755 {} "$LIBEXEC/" \;
    write_file "$DBUS_POLICY" <<'EOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="root">
    <allow own_prefix="org.freedesktop.NetworkManager.sshuttle"/>
    <allow send_destination="org.freedesktop.NetworkManager.sshuttle"/>
  </policy>
  <policy context="default">
    <deny own_prefix="org.freedesktop.NetworkManager.sshuttle"/>
    <deny send_destination="org.freedesktop.NetworkManager.sshuttle"/>
  </policy>
</busconfig>
EOF
    write_file "/etc/systemd/system/$PLUGIN_UNIT" <<EOF
[Unit]
Description=nm-sshuttle spike: NetworkManager VPN plugin

[Service]
Type=dbus
BusName=$BUS
Environment=NMSS_RECONNECT_DELAY=$RECONNECT_DELAY
ExecStart=$LIBEXEC/nm-sshuttle-spike-service
EOF
    # The tunnel unit, under two names. PartOf= stops it with the plugin unit
    # (shim mode); in direct mode there is no plugin unit and it is inert.
    local u
    for u in "$TUNNEL_UNIT" "$NM_TUNNEL_UNIT"; do
        write_file "/etc/systemd/system/$u" <<EOF
[Unit]
Description=nm-sshuttle spike: sshuttle tunnel
PartOf=$PLUGIN_UNIT

[Service]
Type=notify
NotifyAccess=main
ExecStartPre=$LIBEXEC/sweep
ExecStart=$LIBEXEC/exec-tunnel
ExecStopPost=$LIBEXEC/sweep
Restart=no
TimeoutStartSec=45
TimeoutStopSec=15
EOF
    done
    restorecon -RF "$LIBEXEC" "$DBUS_POLICY" "/etc/systemd/system/$PLUGIN_UNIT" \
        "/etc/systemd/system/$TUNNEL_UNIT" "/etc/systemd/system/$NM_TUNNEL_UNIT" 2> /dev/null || true
    systemctl daemon-reload
    dbus_reload
    raw install ls -lZ "$LIBEXEC" "$DBUS_POLICY" "/etc/systemd/system/$PLUGIN_UNIT" \
        "/etc/systemd/system/$TUNNEL_UNIT" "/etc/systemd/system/$NM_TUNNEL_UNIT"
    result R1e-l INFO "SELinux label of $NM_TUNNEL_UNIT: $(stat -c %C "/etc/systemd/system/$NM_TUNNEL_UNIT" 2> /dev/null || echo ?)"
}

write_name_file() {  # write_name_file direct|shim [auth-dialog: yes|no]
    local program=$LIBEXEC/nm-sshuttle-spike-service auth=${2:-yes}
    if [ "$1" = shim ]; then
        program=$LIBEXEC/nm-sshuttle-spike-activate
        write_file "$DBUS_SERVICE" <<EOF
[D-BUS Service]
Name=$BUS
Exec=$LIBEXEC/nm-sshuttle-spike-service
User=root
SystemdService=$PLUGIN_UNIT
EOF
        restorecon -F "$DBUS_SERVICE" 2> /dev/null || true
    else
        rm -f "$DBUS_SERVICE"
    fi
    dbus_reload
    rm -f "$NAME_FILE"
    sleep 1
    {
        echo "[VPN Connection]"
        echo "name=sshuttle"
        echo "service=$BUS"
        echo "program=$program"
        echo "supports-multiple-connections=false"
        # NetworkManager >= 1.58 refuses private (connection.permissions) profiles without it
        echo "supports-safe-private-file-access=true"
        if [ "$auth" = yes ]; then
            echo
            echo "[GNOME]"
            echo "auth-dialog=$LIBEXEC/nm-sshuttle-spike-auth-dialog"
        fi
    } | write_file "$NAME_FILE"
    restorecon -F "$NAME_FILE" 2> /dev/null || true
    sleep 2   # NetworkManager watches the directory
    raw "name-$1" cat "$NAME_FILE"
}

# profile_data DNS DOMAINS RECONNECT [TUNDEV: dns|always] [UNIT]
profile_data() {
    local d="remote = $USER_NAME@$JUMP_IP, local-user = $USER_NAME, subnets = 10.99.0.0/24, dns = $1"
    [ "$1" = none ] || d="$d, dns-servers = $DNS_IP, dns-domains = $2"
    printf '%s, spike-reconnect = %s, spike-tundev = %s, spike-tunnel-unit = %s%s' \
        "$d" "$3" "${4:-dns}" "${5:-$UNIT}" "${PROFILE_EXTRA:+, $PROFILE_EXTRA}"
}

create_profile() {
    nmcli connection delete "$CON" > /dev/null 2>&1
    # The short vpn-type "sshuttle" must resolve through the .name file alone.
    nmcli connection add type vpn con-name "$CON" vpn-type sshuttle \
        connection.autoconnect no connection.permissions "user:$USER_NAME" \
        ipv4.auto-route-ext-gw no vpn.persistent no \
        vpn.data "$(profile_data none "" identical)" > "$REPORT/raw/profile.txt" 2>&1
    local st
    st=$(nmcli -g vpn.service-type connection show "$CON" 2>&1)
    check R1c "nmcli resolves vpn-type 'sshuttle' without an editor plugin" test "$st" = "$BUS"
    if [ "$st" != "$BUS" ]; then
        { echo "vpn.service-type: $st"; cat "$REPORT/raw/profile.txt"; ls -l "$NAME_FILE"; } 2>&1 |
            detail R1c "service type of the created profile"
    fi
    raw profile nmcli connection show "$CON"
}

# set_profile DNS DOMAINS RECONNECT PERSISTENT [TUNDEV] [UNIT]
set_profile() {
    nmcli connection modify "$CON" vpn.persistent "$4" \
        vpn.data "$(profile_data "$1" "$2" "$3" "${5:-dns}" "${6:-$UNIT}")"
}

activate() {  # activate LOGNAME -> status; output in raw/LOGNAME.txt and $ACTIVATE_OUT
    ACTIVATE_OUT=$(timeout 120 nmcli --wait 90 connection up "$CON" 2>&1)
    printf '$ nmcli connection up %s\n%s\n\n' "$CON" "$ACTIVATE_OUT" >> "$REPORT/raw/$1.txt"
    if contains "$ACTIVATE_OUT" "was not installed" && [ ! -f "$VARDIR/nm-restarted" ]; then
        result R1d INFO "NetworkManager had not picked up the .name file; restarting it once"
        touch "$VARDIR/nm-restarted"
        systemctl restart NetworkManager
        nm-online -s -q -t 60
        sleep 2
        ACTIVATE_OUT=$(timeout 120 nmcli --wait 90 connection up "$CON" 2>&1)
        printf '$ nmcli connection up %s (after NM restart)\n%s\n\n' "$CON" "$ACTIVATE_OUT" \
            >> "$REPORT/raw/$1.txt"
    fi
    [ "$(con_state)" = activated ]
}
activation_failed() {  # activation_failed ID TITLE EPOCH : FAIL plus details
    result "$1" FAIL "$2 (state: $(con_state))"
    { echo "nmcli: $ACTIVATE_OUT"; echo; echo "journal:"; nm_journal "$3"; } | detail "$1" "$2"
}

nm_pid() { systemctl show -p MainPID --value NetworkManager; }
check_nm_alive() {  # check_nm_alive ID EPOCH PID-BEFORE
    if [ "$(nm_pid)" = "$3" ] &&
        ! journalctl --since "@$2" --no-pager -u NetworkManager -o cat | grep -q 'dumped core'; then
        result "$1" PASS "NetworkManager did not crash or restart"
    else
        result "$1" FAIL "NetworkManager crashed or restarted (pid $3 -> $(nm_pid))"
        journalctl --since "@$2" --no-pager -o cat | grep -E -A 12 'assert|should not be reached|dumped core|Stack trace' |
            detail "$1" "NetworkManager crash"
    fi
}

nm_trace_on() {  # nm_trace_on ID
    local err
    nmcli -t -f LEVEL,DOMAINS general logging > "$VARDIR/nm-logging" 2> /dev/null
    # KEEP leaves the other domains as they are; l3cfg logs under CORE.
    if ! err=$(nmcli general logging level KEEP \
        domains VPN:TRACE,CORE:TRACE,DEVICE:DEBUG,DNS:DEBUG,FIREWALL:DEBUG,DISPATCH:DEBUG 2>&1); then
        result "$1-trace" INFO "could not enable NetworkManager trace logging: $err"
    fi
}
nm_trace_off() {
    if [ -s "$VARDIR/nm-logging" ]; then
        # terse output escapes per-domain levels as DOMAIN\:LEVEL
        # shellcheck disable=SC2046
        nmcli general logging level $(cut -d: -f1 "$VARDIR/nm-logging") \
            domains "$(cut -d: -f2- "$VARDIR/nm-logging" | sed 's/\\:/:/g')" 2> /dev/null
        rm -f "$VARDIR/nm-logging"
    fi
}

# ---------------------------------------------------------------- phases
phase_bridge() {
    note "R0 ssh as $USER_NAME through the bridge"
    check R0c "ssh as $USER_NAME via ssh-as-user, key from the user's agent" \
        "$LIBEXEC/ssh-as-user" "$USER_NAME" "$USER_NAME@$JUMP_IP" true
}

phase_activation() {  # phase_activation direct|shim ID [UNIT]
    local mode=$1 id=$2 unit=${3:-$TUNNEL_UNIT} t0 off
    note "R1 activation, mode=$mode, tunnel unit $unit"
    deactivate
    write_name_file "$mode"
    set_profile none "" identical no dns "$unit"
    t0=$(date +%s)
    off=$(audit_offset)
    if activate "activate-$id"; then
        result "$id" PASS "VPN activates with the plugin started $mode ($unit)"
        check "$id-t" "traffic to the internal network goes through the tunnel" http_ok "http://$WEB_IP:8080/"
        result "$id-c" INFO "plugin pid $(plugin_pid), unit $(plugin_field Unit), SELinux $(plugin_field Label)"
        raw "activate-$id" busctl --system --no-pager status "$BUS"
        raw "activate-$id" systemctl status "$unit" --no-pager
        raw "activate-$id" ps -e -o label,user,pid,args
        local tpid
        tpid=$(systemctl show -p MainPID --value "$unit")
        result "$id-p" INFO "sshuttle (tunnel unit main pid $tpid): $(ps -o user=,label= -p "$tpid" | xargs)"
        result "$id-s" INFO "ssh: $(ps -e -o user=,label=,args= | awk '/ssh -o BatchMode/ && !/awk/ {print $1, $2; exit}')"
        check "$id-r" "NM did not route the jump host away from its link" \
            sh -c "ip route get $JUMP_IP | grep -q 'dev h-j'"
        deactivate
        check "$id-d" "deactivation stops the tunnel and removes its rules" \
            sh -c "! systemctl is-active --quiet $unit && [ -z \"\$(nft list tables | grep sshuttle-)\" ]"
    else
        activation_failed "$id" "VPN activates with the plugin started $mode ($unit)" "$t0"
        local secs=$(( $(date +%s) - t0 ))
        result "$id-f" INFO "failure reported to NetworkManager after ${secs} s"
        deactivate
    fi
    local n
    n=$(avc_count_since "$t0" "$off" "$id")
    if [ "$n" = 0 ]; then
        result "R2-$id" PASS "no SELinux denials ($mode, $unit)"
    elif [ "$n" = "?" ]; then
        result "R2-$id" INFO "could not read the audit log, see raw/avc-$id.txt"
    else
        result "R2-$id" FAIL "$n SELinux denial records ($mode, $unit), see raw/avc-$id.txt"
    fi
    raw "journal-$id" journalctl --since "@$t0" --no-pager -u NetworkManager -u "$PLUGIN_UNIT" -u "$unit"
}

phase_dns() {  # phase_dns DNS DOMAINS ID [TUNDEV]
    local dns=$1 domains=$2 id=$3 tundev=${4:-dns} t0
    note "R3 per-tunnel link: dns=$dns domains=${domains:-none} tundev=$tundev"
    deactivate
    set_profile "$dns" "$domains" identical no "$tundev"
    t0=$(date +%s)
    if ! activate "dns-$id"; then
        activation_failed "$id" "VPN with dns=$dns activates" "$t0"
        deactivate
        return
    fi
    result "$id" PASS "VPN with dns=$dns activates (link: $(cat "$STATE/link-kind" 2> /dev/null || echo ?))"
    raw "dns-$id" ip -d addr show nmss0
    raw "dns-$id" resolvectl status nmss0
    raw "dns-$id" resolvectl status
    raw "dns-$id" nmcli device show nmss0
    raw "dns-$id" nmcli -f NAME,TYPE,DEVICE,STATE connection show --active
    raw "dns-$id" firewall-cmd --get-zone-of-interface=nmss0
    raw "dns-$id" grep -E '^(nameserver|search)' /etc/resolv.conf
    check "$id-a" "NM put 192.0.0.8/32 on nmss0" sh -c "ip -4 -o addr show dev nmss0 | grep -q '192.0.0.8/32'"
    result "$id-e" INFO "nmcli device nmss0: $(nmcli -g GENERAL.STATE,GENERAL.CONNECTION device show nmss0 2>&1 | xargs)"
    result "$id-z" INFO "firewalld zone of nmss0: $(fw_zone_of_nmss0)"
    if [ "$dns" = none ]; then
        check "$id-b" "no DNS server on nmss0 for dns=none" sh -c "! resolvectl dns nmss0 | grep -q '[0-9]\.[0-9]'"
        check "$id-t" "traffic goes through the tunnel" http_ok "http://$WEB_IP:8080/"
        deactivate
        check "$id-j" "deactivation removes nmss0" test ! -e /sys/class/net/nmss0
        return
    fi
    check "$id-b" "resolved has $DNS_IP as nmss0's DNS server" sh -c "resolvectl dns nmss0 | grep -q '$DNS_IP'"
    result "$id-c" INFO "resolvectl domain nmss0: $(resolvectl domain nmss0 2>&1 | sed 's/^Link [0-9]* (nmss0)://' | xargs)"
    result "$id-d" INFO "resolvectl default-route nmss0: $(resolvectl default-route nmss0 2>&1 | awk '{print $NF}')"
    local q
    q=$(resolvectl query --legend=no git.corp.test 2>&1)
    echo "$q" >> "$REPORT/raw/dns-$id.txt"
    check "$id-f" "resolvectl query git.corp.test -> $WEB_IP" contains "$q" "$WEB_IP"
    check "$id-g" "the query reached the internal DNS server via the jump host" \
        sh -c "journalctl -u nmss-spike-dns --since @$t0 -o cat --no-pager | grep -q 'query from 10.99.0.1: git.corp.test'"
    check "$id-h" "http://git.corp.test:8080/ works by name through the tunnel" http_ok "http://git.corp.test:8080/"
    q=$(resolvectl query --legend=no git 2>&1 | head -n 1)
    result "$id-s" INFO "single-label 'git': $q"
    q=$(resolvectl query --legend=no fedoraproject.org 2>&1 | head -n 1)
    result "$id-i" INFO "other names still resolve outside the tunnel: $q"
    deactivate
    check "$id-j" "deactivation removes nmss0" test ! -e /sys/class/net/nmss0
}

# NM binds nmss0 to the profile's zone, or to firewalld's default zone if the
# profile names none; a zone name alone cannot tell the two apart (run 5).
fw_zone_of_nmss0() {
    local zone def bound
    command -v firewall-cmd > /dev/null || { echo "firewall-cmd is not installed"; return; }
    def=$(firewall-cmd --get-default-zone 2>&1)
    if ! zone=$(firewall-cmd --get-zone-of-interface=nmss0 2>&1); then
        echo "none ($zone); firewalld's default zone is $def"
        return
    fi
    bound=$(firewall-cmd --zone="$zone" --query-interface=nmss0 2>&1)
    if [ "$zone" = "$def" ]; then
        echo "$zone, firewalld's default zone (bound: $bound)"
    else
        echo "$zone, an explicit zone; firewalld's default zone is $def (bound: $bound)"
    fi
}

plugin_signals() {  # plugin_signals BUSMON_FILE -> "1 Config, 2 Ip4Config, StateChanged 3 4"
    local f=$1 states
    states=$(awk '/Interface=org.freedesktop.NetworkManager.VPN.Plugin  Member=StateChanged/ {s=1; next}
        s && /UINT32/ {v = $2; sub(/;/, "", v); printf "%s ", v; s = 0}' "$f")
    printf '%s Config, %s Ip4Config, StateChanged %s' \
        "$(grep -c 'VPN.Plugin  Member=Config$' "$f")" \
        "$(grep -c 'VPN.Plugin  Member=Ip4Config$' "$f")" "$(echo "${states:-none}" | xargs)"
}

# While NM shows a stuck reconnect: does an unrelated change on nmss0 flip
# it to "activated"? NM never clears its wait-for-pre-up flag (design §2.1).
early_flip_probe() {  # early_flip_probe ID
    local r=""
    ip addr add 192.0.2.99/32 dev nmss0 2> /dev/null
    sleep 3
    r="address added: $(con_state)"
    ip addr del 192.0.2.99/32 dev nmss0 2> /dev/null
    sleep 3
    r="$r; removed: $(con_state)"
    local out
    out=$(nmcli device reapply nmss0 2>&1 | tail -n 1)
    sleep 3
    r="$r; device reapply ('$out'): $(con_state)"
    result "$1-i" INFO "early flip while stuck: $r"
}

watch_reconnect() {  # watch_reconnect LOGNAME -> prints the state sequence
    local prev="" s i seq="" t0 pid
    pid=$(plugin_pid)
    t0=$(date +%s)
    kill -USR1 "$pid"
    for i in $(seq 1 $(( (RECONNECT_DELAY + 30) * 2 ))); do
        s=$(con_state)
        s=${s:-gone}
        if [ "$s" != "$prev" ]; then
            seq="$seq $s@+$(( $(date +%s) - t0 ))s"
            prev=$s
        fi
        if [ "$i" = $(( RECONNECT_DELAY )) ]; then   # halfway through the gap
            raw "$1" resolvectl dns nmss0
            raw "$1" curl -s -m 3 -o /dev/null -w '%{http_code}\n' --noproxy '*' "http://$WEB_IP:8080/"
        fi
        [ "$s" = activated ] && [ "$i" -gt $(( RECONNECT_DELAY * 2 + 4 )) ] && break
        [ "$s" = gone ] && break
        sleep 0.5
    done
    echo "$seq" | xargs
}

# phase_reconnect MODE ID [DNS] [TUNDEV] [EXPECT: recover|stay|stuck]
phase_reconnect() {
    local mode=$1 id=$2 dns=${3:-split} tundev=${4:-dns} expect=${5:-recover} cycles=${6:-1}
    local probe=${7:-} seq t0 nmpid busmon
    note "R4 simulated drop and reconnect: mode=$mode dns=$dns tundev=$tundev (${RECONNECT_DELAY}s gap)"
    deactivate
    local domains=corp.test
    [ "$dns" = none ] && domains=""
    set_profile "$dns" "$domains" "$mode" yes "$tundev"
    t0=$(date +%s)
    nmpid=$(nm_pid)
    if ! activate "reconnect-$id"; then
        activation_failed "$id" "VPN activates before the reconnect test ($mode)" "$t0"
        deactivate
        return
    fi
    if [ "$mode" = nbns ] && [ "$dns" = split ] && [ "$GUI" = 1 ] && [ "$AUTO" = 0 ]; then
        ask "Open Quick Settings in the VM window and watch the VPN toggle. Press Enter to start the reconnect."
        echo
    fi
    result "$id-y" INFO "nmss0 device: $(nmcli -g GENERAL.STATE device show nmss0 2>&1 | head -n 1)"
    local ifi0
    ifi0=$(nmss0_ifindex)
    nm_trace_on "$id"
    busctl --system monitor --match "sender=$BUS" > "$REPORT/raw/busmon-$id.txt" 2>&1 &
    busmon=$!
    local tdrop
    tdrop=$(date +%s)
    seq=$(watch_reconnect "reconnect-$id")
    sleep 1
    kill "$busmon" 2> /dev/null
    wait "$busmon" 2> /dev/null
    journalctl --since "@$tdrop" --no-pager -o short-precise -u NetworkManager > "$VARDIR/trace-drop.txt"
    result "$id-s" INFO "NM states after the drop: $seq"
    result "$id-m" INFO "plugin signals after the drop: $(plugin_signals "$REPORT/raw/busmon-$id.txt")"
    local last=${seq##* } outcome
    if contains "$seq" gone; then
        outcome=deactivated
    elif contains "$seq" activating@ && [ "${last%@*}" = activated ]; then
        outcome=recover
    elif ! contains "$seq" activating@; then
        outcome=stay
    else
        outcome=stuck
    fi
    if [ "$outcome" = "$expect" ]; then
        result "$id" PASS "reconnect mode $mode: $outcome, as expected"
    else
        result "$id" FAIL "reconnect mode $mode: $outcome, expected $expect"
    fi
    check "$id-t" "traffic flows again after the reconnect" http_ok "http://$WEB_IP:8080/"
    if [ "$outcome" = recover ]; then
        # -w, -e, -u and -o pass trivially when nmss0 was replaced: NM's VPN
        # DNS entry stays on the old ifindex, and the new link has no DNS
        # (R4f). They still run, and only a PASS turns into INFO.
        local why whyu
        why=$(relink_why "$ifi0")
        check "$id-v" "NM became activated only after the plugin's new Config (no early flip)" \
            activated_after_config "$VARDIR/trace-drop.txt"
        case $mode in nbns*) result "$id-k" INFO "sentinel committed by NM: $(sentinel_committed "$VARDIR/trace-drop.txt")" ;; esac
        check_unless_trivial "$id-w" "no sentinel WINS server left after the reconnect" "$why" no_wins_sentinel
        if [ "$dns" = split ]; then
            check "$id-d" "nmss0 still has $DNS_IP as DNS server" link_has_dns
            check_unless_trivial "$id-e" "nmss0 is not resolved's default DNS route, and $UPLINK keeps its servers" \
                "$why" resolved_split_ok
            check "$id-q" "git.corp.test resolves through the tunnel after the reconnect" split_name_resolves
            raw "reconnect-$id" nm_dns_config
            check "$id-n" "NM's DNS configuration has exactly one VPN entry for $DNS_IP, on nmss0" vpn_dns_registered_once
            whyu=$(entry_why "$why")
            check_unless_trivial "$id-u" "NM has no non-VPN DNS entry for nmss0 (risk 11)" "$whyu" no_nmss0_device_entry
            [ "$probe" = probe ] && no_default_route_probe "$id" "" "$whyu"
        elif [ "$probe" = probe ]; then
            no_default_route_probe "$id" "" "$why"
        fi
        local i ok=0 tnext rec
        for i in $(seq 2 "$cycles"); do
            tnext=$(date +%s)
            seq=$(watch_reconnect "reconnect-$id")
            result "$id-r$i" INFO "drop $i: $seq"
            last=${seq##* }
            rec=no
            if [ "${last%@*}" = activated ] && contains "$seq" activating@; then
                ok=$(( ok + 1 ))
                rec=yes
            fi
            # Risk 11 after every drop, not only the first (run 5 read only drop 1).
            journalctl --since "@$tnext" --no-pager -o short-precise -u NetworkManager > "$VARDIR/trace-drop$i.txt"
            case $mode in nbns*) result "$id-k$i" INFO "drop $i: sentinel committed by NM: $(sentinel_committed "$VARDIR/trace-drop$i.txt")" ;; esac
            [ "$dns" = split ] || continue
            if [ "$rec" != yes ]; then
                result "$id-u$i" INFO "not evaluated: drop $i did not recover"
                [ "$probe" = probe ] && result "$id-r$i-o" INFO "not evaluated: drop $i did not recover"
                continue
            fi
            raw "reconnect-$id" nm_dns_config
            whyu=$(entry_why "$(relink_why "$ifi0")")
            check_unless_trivial "$id-u$i" "after drop $i, NM has no non-VPN DNS entry for nmss0 (risk 11)" \
                "$whyu" no_nmss0_device_entry
            [ "$probe" = probe ] && no_default_route_probe "$id-r$i" "reconnect-$id" "$whyu"
        done
        [ "$cycles" -gt 1 ] && result "$id-r" "$( [ "$ok" = $(( cycles - 1 )) ] && echo PASS || echo FAIL)" \
            "$(( cycles - 1 )) more drops: $ok recovered"
        if [ "$cycles" -gt 1 ] && [ "$dns" = split ]; then
            check "$id-n2" "after $cycles drops, still exactly one VPN DNS entry, on nmss0" vpn_dns_registered_once
        fi
    fi
    [ "$outcome" = stuck ] && early_flip_probe "$id"
    nm_trace_off
    check_nm_alive "$id-c" "$t0" "$nmpid"
    raw "trace-$id" journalctl --since "@$t0" --no-pager -o short-precise -u NetworkManager
    if [ "$mode" = nbns ] && [ "$dns" = split ]; then
        raw "reconnect-$id" nmcli -f IP4 connection show "$CON"
        if [ "$GUI" = 1 ] && [ "$AUTO" = 0 ]; then
            local a
            a=$(ask "Did the VPN toggle stay on, with its icon changing while it reconnected, and no 'Connection failed' notification? [y/n]")
            if yes_answer "$a"; then result R5e PASS "GNOME showed the reconnect without a notification"; else result R5e FAIL "GNOME reconnect display: answer '$a'"; fi
        fi
    fi
    deactivate
}

# Risk 11: with no default route on any link, does nmss0's DNS take resolved's
# default route ("~.")? Changes only the uplink's applied connection; reapply
# restores it.
no_default_route_probe() {  # no_default_route_probe ID [RAWFILE] [WHY a PASS is trivial]
    local dev=$UPLINK pub corp rf=${2:-reconnect-$1} why=${3:-}
    if [ -z "$dev" ]; then
        result "$1-o" INFO "no IPv4 default route to remove"
        return
    fi
    echo "$dev" > "$VARDIR/uplink-modified"
    raw "$rf" nmcli device modify "$dev" ipv4.never-default yes ipv6.never-default yes
    sleep 3
    raw "$rf" resolvectl dns
    raw "$rf" resolvectl domain
    raw "$rf" resolvectl default-route
    pub=$(resolvectl query --legend=no --cache=no fedoraproject.org 2>&1 | head -n 1)
    corp=$(resolvectl query --legend=no --cache=no git.corp.test 2>&1 | head -n 1)
    check_unless_trivial "$1-o" "without a default route on $dev, nmss0 does not become resolved's default DNS route and $dev keeps its servers" \
        "$why" resolved_split_ok
    result "$1-o2" INFO "lookups without a default route: fedoraproject.org: $pub; git.corp.test: $corp"
    raw "$rf" nm_dns_config
    raw "$rf" nmcli device reapply "$dev"
    sleep 3
    raw "$rf" ip -4 route show default dev "$dev"
    raw "$rf" ip -6 route show default dev "$dev"
    raw "$rf" resolvectl default-route "$dev"
    check "$1-o3" "$dev's DNS servers, its IPv4 default route and its resolved default route are back after the probe" \
        uplink_restored
    rm -f "$VARDIR/uplink-modified"
}

# R4g: the nbns reconnect without firewalld. NM then completes the IP config
# through an idle callback instead of a firewalld zone call.
phase_reconnect_nofw() {  # phase_reconnect_nofw ID [probe]
    if ! systemctl is-active --quiet firewalld; then
        result "$1" SKIP "firewalld is not running, so R4b already covered this"
        return
    fi
    touch "$VARDIR/firewalld-stopped"
    systemctl stop firewalld
    phase_reconnect nbns "$1" split dns recover 1 "${2:-}"
    systemctl start firewalld
    rm -f "$VARDIR/firewalld-stopped"
}

# R4h: the user switches the VPN off while it is reconnecting.
phase_toggle_off_gap() {
    note "R4h switching off during a reconnect"
    deactivate
    # Keep the stopped plugin alive past its reconnect timer (run 3's exited first).
    PROFILE_EXTRA="spike-idle-quit = 60" set_profile split corp.test nbns yes
    local t0 n0 pid i nmpid
    t0=$(date +%s)
    nmpid=$(nm_pid)
    if ! activate reconnect-R4h; then
        activation_failed R4h "VPN activates before the toggle-off test" "$t0"
        deactivate
        return
    fi
    pid=$(plugin_pid)
    n0=$(wc -l < "$STATE/plugin.log")
    kill -USR1 "$pid"
    sleep 3
    result R4h-g INFO "state during the gap: $(con_state)"
    raw reconnect-R4h nmcli connection down "$CON"
    # Watch past the plugin's scheduled reconnect: nothing may come back.
    local seq="" prev="" st
    for i in $(seq 1 $(( RECONNECT_DELAY + 10 ))); do
        st=$(con_state)
        st=${st:-gone}
        [ "$st" != "$prev" ] && seq="$seq $st@+${i}s" && prev=$st
        sleep 1
    done
    result R4h-s INFO "states after switching off:$seq"
    tail -n +"$n0" "$STATE/plugin.log" > "$REPORT/raw/reconnect-R4h-plugin.txt"
    check R4h "switching off during a reconnect ends it for good" con_gone
    check R4h-n "the plugin sent no config after Disconnect" \
        no_config_after_disconnect "$REPORT/raw/reconnect-R4h-plugin.txt"
    check R4h-c "tunnel, nmss0 and sshuttle rules are gone" tunnel_cleaned
    # Run 5's 'busctl status' failed while plugin.log showed the plugin alive
    # (a harness defect). Test the PID and its bus name instead, and record
    # busctl status once, without a pager, with its exit code.
    {
        echo "\$ date +%T.%N; kill -0 $pid; GetNameOwner; busctl --system --no-pager status $BUS"
        date +%T.%N
        if kill -0 "$pid" 2> /dev/null; then echo "pid $pid: alive"; else echo "pid $pid: gone"; fi
        dbus_call GetNameOwner s "$BUS" 2>&1
        busctl --system --no-pager status "$BUS" 2>&1
        echo "exit=$?"
        echo
    } >> "$REPORT/raw/reconnect-R4h.txt"
    check R4h-p "the plugin was still running, and owned its bus name, when its reconnect would have fired" \
        plugin_owns_bus "$pid"
    check R4h-k "the plugin cancelled its pending reconnect at Disconnect" \
        grep -q 'cancelled the pending reconnect' "$REPORT/raw/reconnect-R4h-plugin.txt"
    result R4h-r INFO "NM: $(grep -o 'active connection [0-9]* is [a-z]* (reason [0-9]*)' "$REPORT/raw/reconnect-R4h-plugin.txt" | tail -n 2 | xargs)"
    check_nm_alive R4h-x "$t0" "$nmpid"
}

# R4j/R4k/R4l: the plugin gives up from "connecting", in three orders (design
# §4.4). NM's policy removes VPN DNS only when the VPN fails from
# ip-config-get..activated; a reconnecting VPN sits in "connect".
phase_giveup() {  # phase_giveup MODE ID
    local mode=$1 id=$2 t0 i nmpid busmon tdrop from
    note "$id giving up after a drop ($mode)"
    deactivate
    if [ "$mode" = giveup ]; then
        set_profile split corp.test "$mode" yes
    else
        # keep nmss0 5 s after NM deactivates, so resolved can be read while it exists
        PROFILE_EXTRA="spike-link-delay = 5" set_profile split corp.test "$mode" yes
    fi
    t0=$(date +%s)
    nmpid=$(nm_pid)
    if ! activate "reconnect-$id"; then
        activation_failed "$id" "VPN activates before the give-up test ($mode)" "$t0"
        deactivate
        return
    fi
    raw "reconnect-$id" nm_dns_config
    local n0
    n0=$(vpn_dns_entries)
    nm_trace_on "$id"
    busctl --system monitor --match "sender=$BUS" > "$REPORT/raw/busmon-$id.txt" 2>&1 &
    busmon=$!
    tdrop=$(date +%s)
    kill -USR1 "$(plugin_pid)"
    for i in $(seq 1 $(( RECONNECT_DELAY + 20 ))); do
        [ -z "$(con_state)" ] && break
        sleep 1
    done
    sleep 1
    if [ -e /sys/class/net/nmss0 ]; then
        raw "reconnect-$id" resolvectl dns nmss0
        check "$id-k" "while nmss0 still exists, resolved lists no VPN DNS server on it" sh -c "! resolvectl dns nmss0 | grep -q '$DNS_IP'"
    else
        result "$id-k" INFO "nmss0 was already gone when NM had failed the VPN"
    fi
    sleep 6
    kill "$busmon" 2> /dev/null
    wait "$busmon" 2> /dev/null
    nm_trace_off
    journalctl --since "@$tdrop" --no-pager -o short-precise -u NetworkManager > "$REPORT/raw/trace-$id.txt"
    raw "reconnect-$id" nm_dns_config
    raw "reconnect-$id" resolvectl dns
    raw "reconnect-$id" resolvectl domain
    # NM skips firewalld's removeInterface when the link is already gone (R4j, R4p)
    raw "reconnect-$id" firewall-cmd --get-zone-of-interface=nmss0
    from=$(grep -o 'set state: failed (was [a-z-]*)' "$REPORT/raw/trace-$id.txt" | tail -n 1 | sed 's/.*(was //; s/)//')
    result "$id-s" INFO "plugin signals: $(plugin_signals "$REPORT/raw/busmon-$id.txt"); NM failed the VPN from '${from:-?}'"
    result "$id-f" INFO "NM re-added the address to a removed link: $(grep -c 'do-add-ip4-address.*failure' "$REPORT/raw/trace-$id.txt") times"
    if [ "$mode" = giveup-linklost ]; then
        # The link loss flips NM to "activated"; the plugin bounces it back, and
        # only a give-up from "connect" tests design §4.4's branch 4.
        check "$id-b" "the give-up after the link loss ran from 'connect' (NM failed the VPN from ip-config-get)" \
            test "$from" = ip-config-get
        # -b passes with or without the flip; the source says link deletion
        # still flips the VPN with nmss0 unmanaged (R8p).
        result "$id-a" INFO "NM flipped the VPN after the link loss: $(grep -q 'set state: pre-up (was connect)' "$REPORT/raw/trace-$id.txt" && echo yes || echo no)"
    fi
    check "$id" "the VPN failed after the give-up" con_gone
    check "$id-d" "NetworkManager dropped the VPN's DNS entry after the give-up" \
        test "$(vpn_dns_entries)" -lt "$n0"
    check "$id-r" "resolved no longer lists $DNS_IP" resolved_dropped
    check "$id-c" "nmss0 and the tunnel are gone" tunnel_cleaned
    check_nm_alive "$id-x" "$t0" "$nmpid"
    clear_leaked_dns "$id"
}

clear_leaked_dns() {  # clear_leaked_dns ID: a leaked entry would spoil later DNS checks
    if [ "$(vpn_dns_entries)" -gt 0 ]; then
        result "$1-z" INFO "restarting NetworkManager to clear the leftover VPN DNS entry"
        systemctl restart NetworkManager
        nm-online -s -q -t 60
        sleep 2
    fi
}

# R4m/R4n: the plugin dies (kill -9) or is stopped (SIGTERM) during a gap.
# NM treats a lost bus name as a disconnect from "connect", which leaks the
# VPN's DNS [src]; on SIGTERM the plugin gives up first (design §4.4).
phase_plugin_dies() {  # phase_plugin_dies kill|stop ID
    local how=$1 id=$2 t0 nmpid n0 pid i tdrop from what
    what=$([ "$how" = kill ] && echo "killed (SIGKILL)" || echo "stopped (systemctl stop)")
    note "$id the plugin is $what during a gap"
    if [ "$how" = stop ] && [ "$PLUGIN_MODE" != shim ]; then
        result "$id" SKIP "needs the shim (the plugin runs in its own unit)"
        return
    fi
    deactivate
    set_profile split corp.test nbns yes
    t0=$(date +%s)
    nmpid=$(nm_pid)
    if ! activate "reconnect-$id"; then
        activation_failed "$id" "VPN activates before the $how test" "$t0"
        deactivate
        return
    fi
    n0=$(vpn_dns_entries)
    pid=$(plugin_pid)
    nm_trace_on "$id"
    tdrop=$(date +%s)
    kill -USR1 "$pid"
    sleep 3
    if [ "$how" = kill ]; then kill -9 "$pid"; else systemctl stop "$PLUGIN_UNIT"; fi
    for i in $(seq 1 30); do
        [ -z "$(con_state)" ] && break
        sleep 1
    done
    sleep 8
    nm_trace_off
    journalctl --since "@$tdrop" --no-pager -o short-precise -u NetworkManager > "$REPORT/raw/trace-$id.txt"
    from=$(grep -o 'set state: \(failed\|disconnected\) (was [a-z-]*)' "$REPORT/raw/trace-$id.txt" | tail -n 1)
    result "$id-s" INFO "NM: ${from:-no state line}"
    check "$id" "the VPN is down after the plugin was $what" con_gone
    raw "reconnect-$id" nm_dns_config
    if [ "$how" = kill ]; then
        result "$id-x0" INFO "expected: NM keeps the VPN's DNS entry after a kill (run 5, R4m); nmss0 stays behind"
    fi
    check "$id-d" "NetworkManager dropped the VPN's DNS entry" test "$(vpn_dns_entries)" -lt "$n0"
    result "$id-l" INFO "nmss0 left behind: $([ -e /sys/class/net/nmss0 ] && echo yes || echo no)"
    ip link del nmss0 2> /dev/null
    check_nm_alive "$id-c" "$t0" "$nmpid"
    clear_leaked_dns "$id"
}

# R8: the same checks with nmss0 unmanaged by the packaged NM configuration
# snippet (design §4.1). R8j, R8p and R8m cover the gaps run 5 left open with
# nmss0 unmanaged: the two-phase sentinel, link loss and a kill in a gap.
phase_unmanaged() {
    note "R8 an unmanaged nmss0"
    deactivate
    printf '[keyfile]\nunmanaged-devices+=interface-name:nmss0\n' | write_file "$NM_UNMANAGED_CONF"
    systemctl restart NetworkManager
    nm-online -s -q -t 60
    sleep 2
    raw unmanaged NetworkManager --print-config
    check R8 "NetworkManager keeps both nmss0 and h-j unmanaged" \
        sh -c "NetworkManager --print-config | grep '^unmanaged-devices=' | grep -q 'interface-name:nmss0' &&
               NetworkManager --print-config | grep '^unmanaged-devices=' | grep -q 'interface-name:h-j'"
    result R8-x INFO "expected: nmss0 'unmanaged' (-y); no non-VPN nmss0 DNS entry (-u) and -o passes, also after R8j's committed sentinel (-k yes); the reapply fails and does not flip R8i; R8p-a yes (link deletion still flips the VPN [src]); R8p-b PASS; R8m-d FAIL (a kill leaks the DNS entry, as R4m)"
    phase_dns split corp.test R8a
    phase_reconnect nbns R8b split dns recover 1 probe
    phase_reconnect_nofw R8g probe
    phase_reconnect perturb R8c split dns recover 1 probe
    phase_reconnect nbns2 R8j split dns recover 1 probe
    phase_reconnect identical R8i split dns stuck
    phase_giveup giveup-config R8l
    phase_giveup giveup-linklost R8p
    phase_plugin_dies kill R8m
    deactivate
    rm -f "$NM_UNMANAGED_CONF"
    systemctl restart NetworkManager
    nm-online -s -q -t 60
    sleep 2
}

# After R4f: does NM still hold a DNS entry for the deleted link?
post_relink_dns_check() {
    raw "reconnect-R4f" nm_dns_config
    result R4f-z INFO "after the relink and Disconnect, NM DNS entries without an interface: $(nm_dns_json | python3 -I -c 'import json, sys; print(sum(1 for e in json.load(sys.stdin)["data"] if "interface" not in e))')"
    clear_leaked_dns R4f
}

phase_nm_restart() {
    note "R7 NetworkManager restarting under an active VPN"
    deactivate
    set_profile split corp.test nbns yes
    local t0
    t0=$(date +%s)
    if ! activate nm-restart; then
        activation_failed R7 "VPN activates before the NetworkManager restart" "$t0"
        deactivate
        return
    fi
    local unit=$UNIT i gone=no
    systemctl restart NetworkManager
    for i in $(seq 1 30); do
        if ! bus_owned &&
            ! systemctl is-active --quiet "$unit" && [ ! -e /sys/class/net/nmss0 ] &&
            [ -z "$(sshuttle_tables)" ]; then
            gone=yes
            break
        fi
        sleep 0.5
    done
    if [ "$gone" = yes ]; then
        result R7 PASS "plugin, tunnel, nmss0 and nft rules gone $(( $(date +%s) - t0 )) s after the NM restart"
    else
        result R7 FAIL "leftovers 15 s after the NM restart: plugin=$(bus_owned && echo up || echo gone) tunnel=$(systemctl is-active "$unit") nmss0=$([ -e /sys/class/net/nmss0 ] && echo present || echo gone) tables='$(sshuttle_tables | xargs)'"
        grep -E 'vanished|tearing|exited' "$STATE/plugin.log" | tail -n 10 | detail R7 "plugin log"
    fi
    nm-online -s -q -t 60
    sleep 2
    deactivate
}

phase_gnome() {
    note "R5 GNOME integration (manual)"
    if [ "$GUI" = 0 ] || [ "$AUTO" = 1 ]; then
        result R5 SKIP "needs a GNOME session and an interactive run"
        return
    fi
    deactivate
    set_profile split corp.test nbns yes
    local t0
    t0=$(date +%s)
    activate gnome-up || { activation_failed R5 "VPN activates before the GNOME checks" "$t0"; return; }
    local a
    a=$(ask "In the VM window, open Quick Settings (top right). Is there a VPN toggle '$CON' and is it ON? [y/n]")
    if yes_answer "$a"; then result R5a PASS "Quick Settings shows the VPN toggle, on"; else result R5a FAIL "Quick Settings toggle: answer '$a'"; fi
    ask "Now switch the '$CON' VPN OFF in Quick Settings, then press Enter."
    check R5b "switching off in GNOME deactivates the VPN" wait_state gone 60
    ask "Now switch it ON again in Quick Settings, then press Enter."
    check R5c "switching on in GNOME activates the VPN through the plugin" wait_state activated 90
    check R5c-t "traffic flows after activating from GNOME" http_ok "http://$WEB_IP:8080/"
    deactivate

    # GNOME Settings: first without an auth dialog (the R5d spinner of run 2),
    # then with the stub.
    write_name_file "$PLUGIN_MODE" no
    t0=$(date +%s)
    a=$(ask "Open Settings > Network and click the gear icon of '$CON' (under VPN). How many seconds until the editor appears? (number, or 'never' after 60 s)")
    result R5f INFO "Settings editor without auth dialog appeared after: ${a:-no answer}"
    journalctl _UID="$UID_U" --since "@$t0" --no-pager -o short-precise 2> /dev/null |
        grep -i -E 'vpn|auth|file_test|filename|secret|networkagent|Unhandled|JS ERROR' | tail -n 40 |
        detail R5f "user journal while opening the editor without an auth dialog"
    ask "Close the editor window, then press Enter."
    write_name_file "$PLUGIN_MODE" yes
    a=$(ask "Click the gear icon of '$CON' again. Does the editor appear at once now? [y/n]")
    if yes_answer "$a"; then result R5d PASS "Settings editor opens at once with the auth-dialog stub"; else result R5d FAIL "Settings editor with the auth-dialog stub: answer '$a'"; fi
    a=$(ask "What does the editor show for the VPN (tabs, fields, error text)? Briefly:")
    result R5d-x INFO "Settings editor content: ${a:-no answer}"
    ask "Close the editor window, then press Enter."
}

gui_session() {  # the user's graphical session id, if any
    local s
    for s in $(loginctl show-user "$USER_NAME" -p Sessions --value 2> /dev/null); do
        case $(loginctl show-session "$s" -p Type --value 2> /dev/null) in
            wayland|x11) echo "$s"; return ;;
        esac
    done
}

phase_agent_unlock() {
    note "R6 key unlock by the agent when ssh runs outside the session (manual)"
    if [ "$GUI" = 0 ] || [ "$AUTO" = 1 ]; then
        result R6 SKIP "needs a GNOME session and an interactive run"
        return
    fi
    if [ "$AGENT_KIND" != gcr ] && [ "$AGENT_KIND" != gnome-keyring ]; then
        result R6 SKIP "agent is $AGENT_KIND, not GNOME's"
        return
    fi
    deactivate
    set_profile split corp.test nbns yes
    rm -f "$HOME_U"/.ssh/nmss_spike_locked*
    as_user ssh-keygen -q -t ed25519 -N "$LOCKED_PASSPHRASE" -C nmss-spike-locked \
        -f "$HOME_U/.ssh/nmss_spike_locked"
    as_user ssh-add -d "$HOME_U/.ssh/nmss_spike_auto.pub" > /dev/null 2>&1
    install -m 644 "$HOME_U/.ssh/nmss_spike_locked.pub" "$SSHD_DIR/authorized_keys/$USER_NAME"
    raw agent-unlock as_user ssh-add -L
    local t0 attempt unlocked=no a
    for attempt in 1 2; do
        echo
        echo "The jump host now accepts only ~/.ssh/nmss_spike_locked, which is passphrase"
        echo "protected and not loaded. The agent should ask for its passphrase in the VM window."
        echo "Passphrase: $LOCKED_PASSPHRASE"
        ask "Press Enter to activate the VPN, then watch the VM window."
        t0=$(date +%s)
        if activate "agent-unlock-$attempt"; then
            result "R6-$attempt" PASS "agent prompted, key unlocked, VPN up (attempt $attempt)"
            unlocked=yes
            break
        fi
        activation_failed "R6-$attempt" "VPN with the locked key comes up (attempt $attempt)" "$t0"
        deactivate
        [ "$attempt" = 2 ] && break
        a=$(ask "Restart gcr-ssh-agent (it may only scan ~/.ssh at start) and retry? [y/n]")
        yes_answer "$a" || break
        as_user systemctl --user restart gcr-ssh-agent.service > /dev/null 2>&1
        sleep 2
    done
    a=$(ask "Did a passphrase prompt appear in the VM window? [y/n]")
    result R6-p INFO "passphrase prompt seen: ${a:-no answer}"

    # R6b: reconnect behind the lock screen with the key no longer loaded. gcr
    # cancels prompts while the screen is locked, so ssh should fail fast.
    if [ "$unlocked" = yes ]; then
        local sid fail_at start_at took stayed_locked=yes
        sid=$(gui_session)
        as_user ssh-add -d "$HOME_U/.ssh/nmss_spike_locked.pub" > /dev/null 2>&1
        if [ -n "$sid" ]; then
            echo
            echo "The VM screen locks now for up to 90 s. Leave it locked and do not touch the"
            echo "VM window until this terminal asks you to unlock it."
        fi
        if [ -n "$sid" ] && loginctl lock-session "$sid"; then
            sleep 3
            start_at=$(( $(date +%s) + RECONNECT_DELAY ))
            kill -USR1 "$(plugin_pid)"
            for _ in $(seq 1 $(( (RECONNECT_DELAY + 60) * 2 ))); do
                [ "$(loginctl show-session "$sid" -p LockedHint --value 2> /dev/null)" = yes ] ||
                    stayed_locked=no
                [ -z "$(con_state)" ] && break
                sleep 0.5
            done
            fail_at=$(date +%s)
            took=$(( fail_at - start_at ))
            if [ "$stayed_locked" != yes ]; then
                result R6b INFO "the screen did not stay locked, so this says nothing (state '$(con_state)' after $took s)"
            elif [ -n "$(con_state)" ]; then
                result R6b FAIL "behind the lock screen the reconnect did not fail within 60 s (state $(con_state))"
            elif [ "$took" -lt 30 ]; then
                result R6b PASS "behind the lock screen the reconnect failed after $took s (no hang)"
            else
                result R6b FAIL "behind the lock screen the reconnect blocked for $took s, until the tunnel unit's start timeout (hang)"
            fi
            grep -E 'reconnect|FAILURE|Permission denied|timeout|agent refused' "$STATE/plugin.log" | tail -n 6 |
                detail R6b "plugin log"
            ask "Unlock the VM screen (password: spike) and press Enter. Dismiss any passphrase prompt that appears."
        else
            result R6b SKIP "could not find or lock the GNOME session"
        fi
    fi
    deactivate
    install -m 644 "$HOME_U/.ssh/nmss_spike_auto.pub" "$SSHD_DIR/authorized_keys/$USER_NAME"
    as_user ssh-add -q "$HOME_U/.ssh/nmss_spike_auto" < /dev/null
}

collect() {
    note "collecting"
    nm_trace_off
    local n
    n=$(avc_count_since "$START_EPOCH" "$START_OFFSET" all)
    result R2 INFO "$n SELinux denial records during the whole run (raw/avc-all.txt)"
    raw journal-all journalctl --since "@$START_EPOCH" --no-pager -o short-precise \
        -u NetworkManager -u "$PLUGIN_UNIT" -u "$TUNNEL_UNIT" -u "$NM_TUNNEL_UNIT" \
        -u nmss-spike-sshd -u nmss-spike-dns -u systemd-resolved \
        -u firewalld -u NetworkManager-dispatcher -u dbus-broker
    raw journal-user journalctl _UID="$UID_U" --since "@$START_EPOCH" --no-pager -o short-precise
    raw firewalld firewall-cmd --get-active-zones
    [ -f "$STATE/plugin.log" ] && cp "$STATE/plugin.log" "$REPORT/raw/plugin.log"
    {
        echo "# nm-sshuttle spike report"
        echo
        echo "Run: $(date -d "@$START_EPOCH" '+%F %T') to $(date '+%T'), user $USER_NAME, $([ "$AUTO" = 1 ] && echo automatic || echo interactive)."
        echo
        echo '```'
        cat "$REPORT/system.txt"
        echo '```'
        echo
        echo "| ID | Result | Check |"
        echo "|---|---|---|"
        local r
        for r in "${RESULTS[@]}"; do
            IFS='|' read -r id st text <<< "$r"
            text=${text//|/\\|}
            echo "| $id | $st | $text |"
        done
        if [ -s "$REPORT/details.md" ]; then
            echo
            echo "## Details"
            echo
            cat "$REPORT/details.md"
        fi
        echo "Raw command output, journals and SELinux records are in raw/."
    } > "$REPORT/report.md"
    chmod -R a+rX "$REPORT"
    echo
    echo "report: $REPORT/report.md"
}

# ------------------------------------------------------------------ main
[ "$KEEP" = 1 ] || trap cleanup EXIT
preflight
cleanup > /dev/null 2>&1   # leftovers of an earlier run
mkdir -p "$REPORT/raw" "$VARDIR"
agent_setup
install_plugin
write_name_file direct
topology
create_profile
phase_bridge

# R1: who may start the tunnel unit. Direct mode with the plain unit name is
# expected to fail (SELinux); with a NetworkManager-* name it should not.
result R1a-x INFO "expected below: R1a FAILS (NetworkManager_t may not start systemd_unit_file_t units); R1e and R1b should pass"
phase_activation direct R1a "$TUNNEL_UNIT"
phase_activation direct R1e "$NM_TUNNEL_UNIT"
phase_activation shim R1b "$TUNNEL_UNIT"
if passed R1b; then
    PLUGIN_MODE=shim UNIT=$TUNNEL_UNIT
elif passed R1e; then
    PLUGIN_MODE=direct UNIT=$NM_TUNNEL_UNIT
elif passed R1a; then
    PLUGIN_MODE=direct UNIT=$TUNNEL_UNIT
else
    echo "no activation mode worked; skipping the remaining phases"
    collect
    exit 1
fi
result R1-m INFO "later phases use the $PLUGIN_MODE plugin with $UNIT"
write_name_file "$PLUGIN_MODE"

phase_dns split corp.test R3a
phase_dns split '~corp.test' R3b
phase_dns none "" R3c always
result R4-x INFO "expected: with a managed nmss0, -u FAIL after content-changing reconnects (R4i, R4c) and R4c-o FAIL (risk 11); R4g-u and R4g-o FAIL only if R4g-k says yes (a race); R4b-u2, -u3, -r2-o and -r3-o PASS (no content change); R4h-p PASS; R4k-d, R4k-k and R4m-d FAIL (DNS leaks); R4f-v, -d, -q and -n FAIL, and R4f-w, -e and -u are INFO unless they FAIL (relink); R4p-a yes; R4p-b PASS; R8 repeats with nmss0 unmanaged"
phase_reconnect identical R4a split dns stuck
phase_reconnect nbns R4b split dns recover 3 probe
phase_reconnect nbns2 R4i split dns recover
phase_reconnect_nofw R4g probe
phase_reconnect perturb R4c split dns recover 1 probe
phase_reconnect invisible R4d split dns stay
phase_reconnect nbns R4e none always recover
phase_toggle_off_gap
phase_giveup giveup R4j
phase_giveup giveup-stopped R4k
phase_giveup giveup-config R4l
phase_giveup giveup-linklost R4p
phase_plugin_dies stop R4n
phase_plugin_dies kill R4m
phase_unmanaged
phase_gnome
phase_agent_unlock
phase_nm_restart
# Last: replacing the tundev crashed NetworkManager 1.56.1 in run 2. This
# variant waits for NetworkManager's device first.
phase_reconnect relink-wait R4f split dns recover
post_relink_dns_check
collect
