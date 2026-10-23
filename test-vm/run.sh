#!/bin/bash
# SPDX-License-Identifier: MIT
#
# M2 scenario driver for the installed nm-sshuttle plugin. Run as root inside
# the throwaway Fedora 44 VM (test-vm/vm.sh does it), after test-vm/install.sh:
#
#   sudo test-vm/run.sh --user tester [--only M2-05,M2-06] [--report DIR] [--keep] [--suspend]
#   sudo test-vm/run.sh cleanup
#
# It builds the spike's topology (jump host sshd, DNS and web in namespaces,
# an agent key for the user), creates a profile with nmcli, and drives the
# REAL plugin (nm-sshuttle@N.service, started by the dispatcher) through the scenarios below. A gap is made
# the way it happens in life: the jump host starts refusing connections and the
# tunnel dies; lifting the block lets the plugin's own retries (1, 2, 4 ... s)
# succeed. Nothing in the plugin is patched or timed.
#
#   M2-01 install       M2-02 connect          M2-03 reconnect, firewalld x3
#   M2-04 no firewalld  M2-05 long gap         M2-06 early "activated"
#   M2-07 link loss     M2-08 SIGTERM/restart  M2-09 kill -9
#   M2-10 off in a gap  M2-11 connect/disconnect  M2-12 user override
#   M2-13 NM restart    M2-14 ExecStopPost     M2-15 SELinux denials (always)
#   M2-16 kill sshuttle while up   M2-17 nft flush ruleset (health)
#   M2-18 link loss while activated   M2-19 link loss before activated
#   M2-20 managed yes in a gap     M2-21 stop in a gap, user override
#   M2-22 reload conf while up     M2-23 roaming between two uplinks
#   M2-24 no default route         M2-25 lock screen (LockedHint set by root)
#   M2-26 park candidate           M2-27 probe with forwarding dead
#   M2-28 suspend and resume (only with --suspend: the VM must wake by RTC)
#   M2-29 a ProxyJump host named inside the tunnelled DNS domain
#   M2-30 dns=split refused with /etc/resolv.conf not resolved's stub
#   V6-01 IPv6 subnet through the tunnel, fail closed in a gap
#   V6-02 roams that change only the uplink's IPv6 addresses
#   TP-01 method=tproxy: UDP and TCP through the tunnel, fail closed in a gap
#   TP-02 method=tproxy: kill sshuttle and nft flush ruleset while up
#   TP-03 method=tproxy: NM leaves the policy routing alone; teardown removes it
#   MT-01 several tunnels: two at once, a reconnect, an overlap, a kill, NM restart
#   MT-02 several tunnels: the lab over corp, back at once after a corp gap
#   MT-03 several tunnels: how long the dispatcher and the instance take to start
#
# Result ids are M2-NN (scenario) and M2-NNx (a check in it). PASS and FAIL are
# asserted; INFO prints something the design marks unobserved. A FAIL prints
# what it saw and is repeated, with resolved's and NM's state and the plugin's
# journal, in details.md of the report.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
# shellcheck source=lib.sh
. "$HERE/lib.sh"

USER_NAME=${SUDO_USER:-}
REPORT=/var/tmp/nm-sshuttle-test-report
ONLY=""
KEEP=0
SUSPEND=0
CMD=run
while [ $# -gt 0 ]; do
    case $1 in
        --user) USER_NAME=$2; shift 2 ;;
        --report) REPORT=$2; shift 2 ;;
        --only) ONLY=$2; shift 2 ;;
        --keep) KEEP=1; shift ;;
        --suspend) SUSPEND=1; shift ;;
        cleanup) CMD=cleanup; shift ;;
        -h|--help) sed -n '3,41p' "$0"; exit 0 ;;
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
SCENARIO=M2-00

if [ "$CMD" = cleanup ]; then
    cleanup
    exit 0
fi

# want M2-NN : is this scenario selected? --only takes M2-05 or M2-05a, comma separated
want() {
    local w
    [ -z "$ONLY" ] && return 0
    for w in ${ONLY//,/ }; do
        [ "${w:0:5}" = "$1" ] && return 0
    done
    return 1
}

# What 'meson install' puts in place (meson.build; NM's directories are fixed under /usr/lib)
INSTALLED=(
    /usr/lib/NetworkManager/VPN/nm-sshuttle-service.name
    /usr/lib/NetworkManager/conf.d/90-nm-sshuttle.conf
    "$LIBEXEC/nm-sshuttle" "$LIBEXEC/nm-sshuttle-service"
    "$LIBEXEC/nm-sshuttle-activate" "$LIBEXEC/nm-sshuttle-auth-dialog"
    "$LIBEXEC/nm-sshuttle-dispatcher"
    /usr/lib/systemd/system/nm-sshuttle@.service
    /usr/lib/systemd/system/nm-sshuttle-tunnel@.service
    /usr/lib/systemd/system/nm-sshuttle-dispatcher.service
    /usr/share/dbus-1/system.d/nm-sshuttle.conf
    /usr/share/dbus-1/system-services/org.freedesktop.NetworkManager.sshuttle.Dispatcher.service
)
PYDIR=""

# ---------------------------------------------------------------- preflight
preflight() {
    note "preflight"
    rm -rf "$REPORT"
    mkdir -p "$REPORT/raw" "$VARDIR"
    START_EPOCH=$(date +%s)
    MARK_EPOCH=$START_EPOCH
    START_OFFSET=$(audit_offset)
    # shellcheck disable=SC1091
    . /etc/os-release
    [ "${ID:-}" = fedora ] || echo "warning: not Fedora (${PRETTY_NAME:-unknown})"
    [ "${VERSION_ID:-}" = 44 ] || echo "warning: written for Fedora 44, this is ${PRETTY_NAME:-unknown}"
    local missing="" c
    for c in nmcli nft ip ss setpriv /usr/sbin/sshd ssh ssh-keygen ssh-keyscan ssh-add busctl \
             resolvectl curl python3 sshuttle systemd-run journalctl restorecon; do
        command -v "$c" > /dev/null || missing="$missing $c"
    done
    [ -z "$missing" ] || { echo "missing:$missing (provision the VM first)" >&2; exit 2; }
    [ -x "$LIBEXEC/nm-sshuttle" ] || { echo "$LIBEXEC/nm-sshuttle is missing: run test-vm/install.sh first" >&2; exit 2; }
    [ -d "$RT_U" ] || { echo "$USER_NAME has no session (no $RT_U)" >&2; exit 2; }
    [ "$UID_U" -ge 1000 ] || { echo "$USER_NAME needs a uid >= 1000 (the plugin refuses system users)" >&2; exit 2; }
    PYDIR=$(cd / && python3 -c 'import os, nm_sshuttle; print(os.path.dirname(nm_sshuttle.__file__))' 2> /dev/null)
    {
        echo "os: ${PRETTY_NAME:-?}"
        echo "kernel: $(uname -r)"
        echo "NetworkManager: $(NetworkManager --version 2> /dev/null)"
        echo "systemd: $(systemctl --version | head -n 1)"
        echo "sshuttle: $(sshuttle --version 2>&1) ($(command -v sshuttle))"
        echo "selinux: $(getenforce 2> /dev/null || echo n/a)"
        echo "resolved: $(systemctl is-active systemd-resolved) ($(readlink /etc/resolv.conf))"
        echo "firewalld: $(systemctl is-active firewalld 2> /dev/null) (default zone $(fw_default_zone 2> /dev/null))"
        echo "plugin: $PYDIR"
        echo "repo: $(cd "$REPO" && git describe --always --dirty 2> /dev/null || echo 'copied, no git')"
    } | tee "$REPORT/system.txt"
    UPLINK=$(ip -4 route show default | awk '{print $5; exit}')
    [ -n "$UPLINK" ] || { echo "no IPv4 default route: the uplink checks need one" >&2; exit 2; }
    echo "uplink: $UPLINK" | tee -a "$REPORT/system.txt"
    # A fresh NetworkManager: it also picks up the .name file and conf.d that meson installed.
    sc restart NetworkManager
    nm-online -s -q -t 60
    sleep 2
    raw system nmcli general status
    raw system nmcli device status
    raw system ls -lZ "${INSTALLED[@]}"
}

# begin ID TITLE : a clean start for one scenario
begin() {
    SCENARIO=$1
    note "$1 $2"
    reset_state
    log_mark
}
# connect_ok ID TEXT [CONNECTION] : activate and record PASS or FAIL
connect_ok() {
    local con=${3:-$CON}
    if activate "$con"; then
        result "$1" PASS "$2"
        return 0
    fi
    result "$1" FAIL "$2 (state '$(con_state "$con")')"
    { echo "nmcli: $ACTIVATE_OUT"; echo; plog | tail -n 40; echo; alog | tail -n 40; } |
        detail "$1" "$2"
    echo "$ACTIVATE_OUT" | head -n 5 | sed 's/^/          /'
    plog | tail -n 8 | sed 's/^/          | /'
    alog | tail -n 12 | sed 's/^/          a /'
    return 1
}
end_checks() {  # end_checks ID : checks every scenario ends with
    check "$1-x" "the plugin logged no internal error" plog_errors
}

# ---------------------------------------------------------------- scenarios
installed_files_present() {
    local f missing=0
    for f in "${INSTALLED[@]}"; do
        if [ ! -e "$f" ]; then echo "missing: $f"; missing=1; fi
    done
    [ -n "$PYDIR" ] || { echo "python3 cannot import nm_sshuttle"; missing=1; }
    return "$missing"
}
labels_ok() {  # the labels policy gives these paths ('meson install' relabels them)
    local out
    ls -dZ "${INSTALLED[@]}" "$PYDIR" 2>&1
    out=$(restorecon -n -v -R "${INSTALLED[@]}" "$PYDIR" 2>&1)
    [ -z "$out" ] || { echo "restorecon would change:"; echo "$out"; return 1; }
}
dummy_state() {  # the NM device state of a new link named $LINK under the current configuration
    local st
    ip link add "$LINK" type dummy && ip link set "$LINK" up || return 1
    wait_for 5 have_dev_state
    st=$(dev_state)
    ip link del "$LINK"
    sleep 0.5
    echo "${st:-none}"
}
unmanaged_in_print_config() { NetworkManager --print-config | grep '^unmanaged-devices=' | grep -q -F "interface-name:nmss*"; }

have_dev_state() { [ -n "$(dev_state)" ]; }
traffic_ok() { http_ok "http://$WEB_IP:8080/" && http_ok "http://git.corp.test:8080/"; }
still_in_gap() { echo "state '$(con_state)', guard $(guard_present && echo present || echo absent), ifindex $(link_ifindex)"; con_is activating && guard_present && [ "$(link_ifindex)" = "$1" ]; }
bounce_signals_ok() { signal_sequence "$1" | grep -q 'StateChanged:4 StateChanged:3 *$'; }
guard_and_refused() { guard_present && refused_fast; }
stays_down() { sleep 3; echo "state '$(con_state)'"; con_gone; }
zone_con_gone() { [ -z "$(con_state "$CON_ZONE")" ]; }
sigterm_logged() { plog_show "SIGTERM during gap" && plog_show "giving up"; }
new_instance_running() { local p; p=$(plugin_pid); echo "MainPID $p, before $1"; [ -n "$p" ] && [ "$p" != 0 ] && [ "$p" != "$1" ]; }
new_instance_started() { plog | grep -o 'starting (pid [0-9]*' | grep -v "pid $1\$" | grep -q .; }
idle_exit_done() { echo "plugin unit: $(sc is-active "$PLUGIN_UNIT"); $BUS owned: $(bus_owned && echo yes || echo no)"; plugin_unit_down && ! bus_owned; }
stays_dead() { sleep 5; echo "running instances: '$(running_slots | xargs)'"; [ -z "$(running_slots)" ] && ! new_instance_started "$1"; }
new_instance_owns() { local p; p=$(plugin_pid); echo "MainPID $p, before $1"; [ "$p" != "$1" ] && plugin_owns_bus "$p"; }
per_activation_name() { echo "$BUS"; [[ $BUS == "$FIXED_BUS".Connection_* ]]; }
no_work_after_disconnect() { ! plog | sed -n '/Disconnect: switching off/,$p' | grep -E 'reconnect attempt [0-9]+ in|Config:|tunnel dropped'; }
tunnel_not_restarted() { ! jc -u "$TUNNEL_UNIT" --since "@$1" -o cat | grep -E -q 'Starting|Started'; }
process_gone() { ! kill -0 "$1" 2> /dev/null; }
link_not_listed() { nmcli -g DEVICE device status; ! nmcli -g DEVICE device status | grep -q "^$LINK\$"; }

s_install() {
    begin M2-01 "install: files, labels, conf.d"
    check M2-01a "all files of meson install are present" installed_files_present
    check M2-01b "SELinux labels are what policy expects (restorecon -n finds nothing to change)" labels_ok
    result M2-01b-i INFO "label of nm-sshuttle@.service: $(stat -c %C /usr/lib/systemd/system/nm-sshuttle@.service 2> /dev/null); of the service script: $(stat -c %C "$LIBEXEC/nm-sshuttle-service" 2> /dev/null)"
    check M2-01c "SELinux is enforcing" test "$(getenforce 2> /dev/null)" = Enforcing
    # The plugin refuses dns=split unless every lookup goes to resolved's stub
    # (design §4.5), so a differently set image would fail every DNS scenario.
    check M2-01g "/etc/resolv.conf lists only resolved's stub ($(readlink /etc/resolv.conf || echo 'a file'))" \
        sh -c "cd / && python3 -c 'import sys; from nm_sshuttle import tunnel; sys.exit(bool(tunnel.resolv_conf_problem()))'"
    # conf.d 90-nm-sshuttle.conf: take it away and a new nmss0 is managed; put it
    # back, 'nmcli general reload conf', and a new nmss0 is unmanaged (state 10)
    local without with
    [ -f "$NM_PACKAGED" ] || { result M2-01d FAIL "$NM_PACKAGED is missing, cannot test the reload"; return; }
    mv -f "$NM_PACKAGED" "$VARDIR/packaged-conf.bak"
    nmcli general reload conf
    sleep 1
    without=$(dummy_state)
    mv -f "$VARDIR/packaged-conf.bak" "$NM_PACKAGED"
    nmcli general reload conf
    sleep 1
    with=$(dummy_state)
    if [ "$without" = 10 ]; then
        result M2-01d INFO "a new $LINK is unmanaged even without 90-nm-sshuttle.conf (something else does it), so the next check says little"
    else
        result M2-01d PASS "without 90-nm-sshuttle.conf a new $LINK is managed (device state $without)"
    fi
    check M2-01e "with it, after 'nmcli general reload conf', a new $LINK is unmanaged (state 10, saw $with)" test "$with" = 10
    check M2-01f "NetworkManager --print-config lists interface-name:nmss* as unmanaged" unmanaged_in_print_config
}

s_connect() {
    begin M2-02 "connect: activation, address, DNS, guard, unit, firewalld"
    connect_ok M2-02 "activation reaches activated" || return
    check M2-02a "$LINK has 192.0.0.8/32" sh -c "ip -4 -o addr show dev $LINK | grep -q '192.0.0.8/32'"
    wait_for 5 plog_has "unmanaged by NetworkManager, as configured"
    check M2-02b "$LINK is unmanaged (device state 10) after the first connect" test "$(dev_state)" = 10
    check M2-02c "plugin log: '$LINK is unmanaged by NetworkManager, as configured'" plog_show "unmanaged by NetworkManager, as configured"
    check M2-02d "plugin log does not contain 'risk 11'" plog_has_not "risk 11"
    dns_checks M2-02e
    check M2-02f "guard table $GUARD_TABLE exists" guard_present
    check M2-02g "the tunnel unit is active" tunnel_active
    check M2-02h "the plugin is alive and owns $BUS" plugin_owns_bus
    check M2-02k "NetworkManager gave the activation its own bus name (--bus-name)" per_activation_name
    if fw_running; then
        check M2-02i "firewalld has $LINK bound to a zone" fw_bound
        result M2-02i-z INFO "zone of $LINK: $(fw_zone_of_link); default zone $(fw_default_zone)"
    else
        result M2-02i SKIP "firewalld is not running"
    fi
    check M2-02j "traffic to $WEB_IP goes through the tunnel (by address and by name)" traffic_ok
    end_checks M2-02
}

# reconnect_cycle ID [GAP_SECONDS] : one drop, a gap, recovery, all the checks
reconnect_cycle() {
    local id=$1 ifi
    ifi=$(link_ifindex)
    if ! gap_begin; then
        result "$id" FAIL "no gap: the VPN did not go to activating after the tunnel died (state '$(con_state)')"
        plog | tail -n 20 | detail "$id" "plugin journal"
        return 1
    fi
    gap_checks "$id" "$ifi"
    sleep "${2:-4}"
    check "$id-a" "the plugin retried during the gap" plog_show "reconnect attempt"
    gap_end
    recovery_checks "$id" "$ifi"
}

s_reconnect_fw() {
    begin M2-03 "reconnect (nbns) x3 with firewalld"
    if ! fw_running; then
        sc start firewalld 2> /dev/null
        sleep 2
    fi
    if ! fw_running; then
        result M2-03 SKIP "firewalld is not available"
        return
    fi
    connect_ok M2-03 "activation before the drops" || return
    local c
    for c in a b c; do
        reconnect_cycle "M2-03$c" 4 || break
    done
    check M2-03r "plugin log has no 'risk 11'" plog_has_not "risk 11"
    result M2-03n INFO "reconnects logged: $(plog | grep -c 'reconnected after')"
    end_checks M2-03
}

s_reconnect_nofw() {
    begin M2-04 "reconnect (nbns) without firewalld"
    if ! command -v firewall-cmd > /dev/null; then
        result M2-04 SKIP "firewalld is not installed, so M2-03 was already the case without it"
        return
    fi
    touch "$VARDIR/firewalld-stopped"
    sc stop firewalld
    connect_ok M2-04 "activation with firewalld stopped" && reconnect_cycle M2-04a 4
    end_checks M2-04
    reset_state
    sc start firewalld
    rm -f "$VARDIR/firewalld-stopped"
}

gap_len_ok() {
    local l
    journal_catch_up
    l=$(plog | grep -o 'reconnected after [0-9.]*' | tail -n 1 | awk '{print $3}')
    echo "the plugin measured ${l:-no reconnect} s"
    awk -v l="${l:-0}" 'BEGIN {exit !(l >= 10)}'
}
s_long_gap() {
    begin M2-05 "a gap longer than 10 s"
    connect_ok M2-05 "activation before the long gap" || return
    local ifi
    ifi=$(link_ifindex)
    gap_begin || { result M2-05g FAIL "no gap (state '$(con_state)')"; return; }
    sleep 14
    check M2-05a "still reconnecting after 14 s: NM activating, guard present, $LINK kept" still_in_gap "$ifi"
    gap_end
    recovery_checks M2-05r "$ifi"
    check M2-05b "the gap lasted at least 10 s" gap_len_ok
    # The 10 s and 60 s timers of design 4.4 start at the reconnect burst, not at the
    # drop, and run only if NM does not show "activated": a long gap cannot trigger them.
    if plog_has "sending the sentinel alone"; then
        result M2-05e INFO "escalation ran: $(plog | grep -F 'sending the sentinel alone' | head -n 1)"
    else
        result M2-05e INFO "no escalation: NM showed activated within 10 s of the burst, as it does on 1.56 (the escalation needs a NM that does not)"
    fi
    result M2-05n INFO "reconnect attempts logged: $(plog | grep -c 'reconnect attempt [0-9]* in')"
    end_checks M2-05
}

s_early_flip() {
    begin M2-06 "early 'activated' during a gap is bounced"
    connect_ok M2-06 "activation before the gap" || return
    local ifi
    ifi=$(link_ifindex)
    gap_begin || { result M2-06g FAIL "no gap (state '$(con_state)')"; return; }
    sleep 2
    signals_start "$REPORT/raw/signals-M2-06.txt"
    nmcli device set "$LINK" managed no > "$REPORT/raw/early-flip.txt" 2>&1
    result M2-06i INFO "nmcli device set $LINK managed no said: '$(xargs < "$REPORT/raw/early-flip.txt")'"
    wait_for 10 plog_has "bouncing it back"
    signals_stop
    check M2-06a "the plugin logged 'NM shows activated during a reconnect; bouncing it back'" plog_show "bouncing it back"
    sleep 1
    check M2-06b "NM shows activating again after the bounce" con_is activating
    result M2-06s INFO "plugin signals during the flip: $(signal_sequence "$REPORT/raw/signals-M2-06.txt")"
    check M2-06c "the bounce is STARTED then STARTING" bounce_signals_ok "$REPORT/raw/signals-M2-06.txt"
    check M2-06d "guard still present and traffic still refused after the bounce" guard_and_refused
    gap_end
    recovery_checks M2-06r "$ifi"
    end_checks M2-06
}

resolved_no_vpn() {
    rv dns
    rv status "$LINK" 2>&1 | head -n 2
    ! rv dns | grep -q "$DNS_IP" && ! rv dns | grep -q "($LINK)"
}
s_link_loss() {
    begin M2-07 "link loss in a gap"
    connect_ok M2-07 "activation before the link loss" || return
    gap_begin || { result M2-07g FAIL "no gap (state '$(con_state)')"; return; }
    local t0 v alt
    t0=$(now_ms)
    ip link del "$LINK"
    if wait_for 10 con_gone; then v=$(( $(now_ms) - t0 )); else v=""; fi
    check_le M2-07a "the VPN goes to disconnected after 'ip link del $LINK'" "$v" 2300 ms
    sleep 1
    check M2-07b "plugin log: give-up branch 4 (the link is gone)" plog_show "giving up (branch 4"
    check M2-07c "resolved has no $LINK link and no $DNS_IP" resolved_no_vpn
    dns_snap
    check M2-07d "NM DnsManager has no VPN entry" dns_no_vpn_entry
    check M2-07e "NM DnsManager has no entry without an interface" dns_no_bare_entry
    check M2-07f "no $LINK, guard or tunnel unit left" wait_for 10 all_clean
    check M2-07h "the VPN stays down (the plugin does not reconnect a lost link)" stays_down
    result M2-07z INFO "firewalld after the link loss: active zones: $(firewall-cmd --get-active-zones 2>&1 | xargs); zone of $LINK: $(fw_zone_of_link) (design 2.1 says the binding stays; not asserted)"
    unblock_jump
    clear_leaked_dns
    # Next: another profile, in a firewalld zone that is not the default one
    if fw_running; then
        alt=work
        [ "$(fw_default_zone)" = work ] && alt=home
        create_profile "$CON_ZONE" "gateway = $JUMP_IP" connection.zone "$alt"
        if connect_ok M2-07i "the next activation works with a profile in zone '$alt' (default zone: $(fw_default_zone))" "$CON_ZONE"; then
            result M2-07j INFO "zone of $LINK now: $(fw_zone_of_link); expected '$alt'"
            dns_checks M2-07k
        fi
        nmcli connection down "$CON_ZONE" > /dev/null 2>&1
        wait_for 20 zone_con_gone
        sleep 1
        result M2-07l INFO "zone of $LINK after switching the second profile off: $(fw_zone_of_link)"
    else
        result M2-07i SKIP "firewalld is not running"
    fi
    end_checks M2-07
}

stop_result_ok() { echo "stop exit status: $(cat "$VARDIR/stop.rc" 2> /dev/null); Result=$(sc show -p Result --value "$PLUGIN_UNIT")"; [ "$(cat "$VARDIR/stop.rc" 2> /dev/null)" = 0 ]; }
signals_in_order() { echo "$1"; [[ $1 =~ Config\ .*Failure:[0-9]+\ .*StateChanged:6 ]]; }
s_sigterm() {
    begin M2-08 "SIGTERM in a gap: systemctl stop, then restart"
    connect_ok M2-08 "activation before the stop" || return
    local seq oldpid stop_pid t0
    gap_begin || { result M2-08g FAIL "no gap (state '$(con_state)')"; return; }
    sleep 2
    signals_start "$REPORT/raw/signals-M2-08.txt"
    ( sc stop "$PLUGIN_UNIT"; echo $? > "$VARDIR/stop.rc" ) &
    stop_pid=$!
    watch_teardown 20
    wait "$stop_pid"
    signals_stop
    seq=$(signal_sequence "$REPORT/raw/signals-M2-08.txt")
    check M2-08a "signals: Config, then Failure, then STOPPED" signals_in_order "$seq"
    check_le M2-08b "the VPN goes down after the stop" "$T_CON" 3000 ms
    check_le M2-08c "$LINK is removed" "$T_LINK" 2500 ms
    check_le M2-08d "the guard is removed" "$T_GUARD" 3000 ms
    check_le M2-08f "the service has exited" "$T_UNIT" 5500 ms
    check M2-08g "systemctl stop succeeded" stop_result_ok
    check M2-08h "plugin log: SIGTERM during gap, then a give-up" sigterm_logged
    result M2-08i INFO "give-up: $(plog | grep -F 'giving up' | tail -n 1 | cut -c1-200)"
    check M2-08j "nothing left: no link, guard, tunnel unit" all_clean
    dns_snap
    check M2-08k "NM DnsManager has no VPN entry" dns_no_vpn_entry
    check M2-08l "resolved has no $DNS_IP" resolved_no_vpn
    end_checks M2-08

    # systemctl restart in a gap
    note "M2-08 restart"
    reset_state
    log_mark
    connect_ok M2-08m "activation before the restart" || return
    oldpid=$(plugin_pid)
    gap_begin || { result M2-08n FAIL "no gap (state '$(con_state)')"; return; }
    sleep 2
    t0=$(now_ms)
    sc restart "$PLUGIN_UNIT"
    result M2-08o INFO "systemctl restart took $(( $(now_ms) - t0 )) ms"
    check M2-08p "the VPN goes down" wait_for 10 con_gone
    check M2-08q "nothing left: no link, guard, tunnel unit" wait_for 10 all_clean
    check M2-08r "a new plugin instance runs" new_instance_running "$oldpid"
    dns_snap
    check M2-08s "NM DnsManager has no VPN entry" dns_no_vpn_entry
    end_checks M2-08t
}

s_kill9() {
    begin M2-09 "kill -9 in a gap"
    connect_ok M2-09 "activation before the kill" || return
    local oldpid seen_first="" seen_any=no n=0 first=1
    gap_begin || { result M2-09g FAIL "no gap (state '$(con_state)')"; return; }
    sleep 2
    oldpid=$(plugin_pid)
    kill -9 "$oldpid"
    # resolved before any 'nmcli general reload dns-rc': does it keep the server on the link?
    while link_exists && [ "$n" -lt 100 ]; do
        if rv dns "$LINK" 2> /dev/null | grep -q "$DNS_IP"; then
            seen_any=yes
            [ "$first" = 1 ] && seen_first=yes
        else
            [ "$first" = 1 ] && seen_first=no
        fi
        first=0
        n=$(( n + 1 ))
        sleep 0.05
    done
    result M2-09i INFO "resolved kept $DNS_IP on $LINK right after the kill: ${seen_first:-link was already gone}; at any of $n samples until the link went: $seen_any"
    check M2-09a "NM sets the VPN disconnected after the kill" wait_for 15 con_gone
    check M2-09b "$LINK, guard and tunnel unit are gone" wait_for 15 all_clean
    # No activation file names a plugin bus name any more, so NM's Disconnect
    # after the kill starts nothing; ExecStopPost has cleaned up
    # (docs/later/multiple-tunnels.md).
    check M2-09c "no new plugin instance starts after the kill" stays_dead "$oldpid"
    check M2-09e "the plugin unit is down and $BUS is free" idle_exit_done
    result M2-09j INFO "ExecStopPost: $(jc -u "$PLUGIN_UNIT" --since "@$MARK_EPOCH" -o cat | grep -F 'nm-sshuttle cleanup' | tail -n 2 | xargs -d '\n' echo | cut -c1-200)"
    dns_snap
    result M2-09l INFO "NM DnsManager after the kill (design expects a leaked VPN entry until NM restarts): $(xargs <<< "$DNS_SNAP")"
    clear_leaked_dns
    end_checks M2-09
}

s_toggle_off() {
    begin M2-10 "switching off while a reconnect is pending"
    connect_ok M2-10 "activation before the gap" || return
    local pid t_off
    pid=$(plugin_pid)
    gap_begin || { result M2-10g FAIL "no gap (state '$(con_state)')"; return; }
    wait_for 10 plog_has "reconnect attempt"
    nmcli connection down "$CON" > "$REPORT/raw/toggle-off.txt" 2>&1
    sleep 3
    t_off=$(date +%s)
    check M2-10a "the VPN is down" con_gone
    check M2-10b "plugin log: Disconnect: switching off" plog_show "Disconnect: switching off"
    sleep 25
    check M2-10c "no reconnect is scheduled, and no Config is sent, after the Disconnect" no_work_after_disconnect
    check M2-10d "the tunnel unit was not started again" tunnel_not_restarted "$t_off"
    check M2-10e "nothing left: no link, guard, tunnel unit" all_clean
    check M2-10f "the plugin served its one activation and exited; $BUS is free" idle_exit_done
    end_checks M2-10
}

s_reconnect_connect() {
    begin M2-11 "Connect right after Disconnect"
    connect_ok M2-11 "first activation" || return
    local pid
    pid=$(plugin_pid)
    # a: asynchronous down, then up at once: the Connect may arrive while the plugin is stopping
    nmcli --wait 0 connection down "$CON" > /dev/null 2>&1
    if connect_ok M2-11a "activation again right behind an asynchronous 'connection down'"; then
        check M2-11b "a new plugin instance (slot $SLOT, $BUS) served it, not pid $pid" new_instance_owns "$pid"
        dns_checks M2-11c
    fi
    # d: synchronous down, then up
    nmcli connection down "$CON" > /dev/null 2>&1
    wait_for 20 con_gone
    if connect_ok M2-11d "activation again right after a finished 'connection down'"; then
        check M2-11e "a new plugin instance (slot $SLOT, $BUS) served it, not pid $pid" new_instance_owns "$pid"
        dns_checks M2-11f
    fi
    check M2-11g "plugin log: no Connect was ignored" plog_has_not "Connect ignored"
    result M2-11h INFO "Connect arrived during teardown (queued): $(plog | grep -c 'Connect arrived during')"
    end_checks M2-11
}

s_resolv_conf() {
    begin M2-30 "dns=split with /etc/resolv.conf linked to resolved's list of the real servers"
    rm -f "$VARDIR/resolv.conf.saved"
    cp -a /etc/resolv.conf "$VARDIR/resolv.conf.saved" || { result M2-30 SKIP "cannot save /etc/resolv.conf"; return; }
    ln -sf ../run/systemd/resolve/resolv.conf /etc/resolv.conf
    activate
    wait_for 20 con_gone
    check M2-30a "the activation is refused" con_gone
    check M2-30b "plugin log: dns=split needs systemd-resolved (resolv.conf or NM's DNS mode)" plog_show "dns=split needs"
    rm -f /etc/resolv.conf
    cp -a "$VARDIR/resolv.conf.saved" /etc/resolv.conf
    restorecon /etc/resolv.conf 2> /dev/null
    result M2-30c INFO "/etc/resolv.conf restored: $(readlink /etc/resolv.conf || echo 'a file')"
    check M2-30d "nothing left: no link, guard, tunnel unit" all_clean
    end_checks M2-30
}

s_override() {
    begin M2-12 "user override of the unmanaged setting"
    printf '[keyfile]\nunmanaged-devices=interface-name:nmtest-nosuch\n' | write_file "$NM_OVERRIDE"
    touch "$VARDIR/override-written"
    nmcli general reload conf
    sleep 1
    result M2-12i INFO "override in effect: unmanaged-devices=$(NetworkManager --print-config | sed -n 's/^unmanaged-devices=//p' | head -n 1)"
    if connect_ok M2-12 "activation with the override in place"; then
        wait_for 5 plog_has "is managed by NetworkManager"
        check M2-12a "plugin log: '$LINK is managed by NetworkManager' (with the risk 11 hint)" plog_show "is managed by NetworkManager"
        check M2-12b "$LINK is not unmanaged (device state $(dev_state), not 10)" test "$(dev_state)" != 10
        dns_snap
        result M2-12c INFO "NM DnsManager with a managed $LINK: $(xargs <<< "$DNS_SNAP")"
    fi
    reset_state
    rm -f "$NM_OVERRIDE" "$VARDIR/override-written"
    nmcli general reload conf
    sleep 1
    log_mark
    if connect_ok M2-12d "activation after the override was removed and the configuration reloaded"; then
        wait_for 5 plog_has "unmanaged by NetworkManager, as configured"
        check M2-12e "plugin log: '$LINK is unmanaged by NetworkManager, as configured'" plog_show "unmanaged by NetworkManager, as configured"
        check M2-12f "plugin log has no 'is managed by NetworkManager'" plog_has_not "is managed by NetworkManager"
        check M2-12g "$LINK is unmanaged (state 10)" test "$(dev_state)" = 10
    fi
    end_checks M2-12
}

s_nm_restart() {
    begin M2-13 "NetworkManager restart under an active VPN"
    connect_ok M2-13 "activation before the restart" || return
    local pid t0 tl="" tg="" tt="" tp=""
    pid=$(plugin_pid)
    t0=$(now_ms)
    sc restart --no-block NetworkManager
    wait_for 15 link_gone && tl=$(( $(now_ms) - t0 ))
    wait_for 15 guard_gone && tg=$(( $(now_ms) - t0 ))
    wait_for 15 tunnel_gone && tt=$(( $(now_ms) - t0 ))
    wait_for 15 not_bus_owned && tp=$(( $(now_ms) - t0 ))
    # the plugin sees NM's name vanish only when NM has stopped, which the restart itself takes a while to do
    check_le M2-13a "$LINK is removed after the restart was requested (plugin removes it first)" "$tl" 3000 ms
    check_le M2-13b "the guard is removed" "$tg" 8000 ms
    check_le M2-13c "the tunnel unit is gone" "$tt" 8000 ms
    check_le M2-13d "the plugin exits (bus name released)" "$tp" 12000 ms
    check M2-13e "the plugin process is gone" process_gone "$pid"
    nm-online -s -q -t 60
    sleep 2
    check M2-13f "the restarted NetworkManager does not list $LINK" link_not_listed
    check M2-13g "the VPN is not active in the restarted NetworkManager" con_gone
    dns_snap
    check M2-13h "NM DnsManager has no VPN entry" dns_no_vpn_entry
    check M2-13i "plugin log: NetworkManager vanished" plog_show "NetworkManager vanished"
    check M2-13j "nothing left: no link, guard, tunnel unit" all_clean
    end_checks M2-13
}

make_leftovers() {
    ip link add "$LINK" type dummy && ip link set "$LINK" up
    nft add table inet "$GUARD_TABLE"
    nft add table inet sshuttle-ipv4-59999   # no listener on that port: stale
}
s_stoppost() {
    begin M2-14 "ExecStopPost cleans up what the plugin left"
    # Started by hand, without a bus name from the dispatcher: the instance
    # falls back to NM's fixed name.
    use_slot 0
    rm -f "$STATE_DIR/0/bus-name"
    # a: leftovers exist before the unit starts; whichever of the plugin's startup
    # cleanup and ExecStopPost removes them, none may remain after start + stop
    make_leftovers
    check M2-14a "the unit starts with leftovers around ($LINK, guard table, stale sshuttle table)" sc start "$PLUGIN_UNIT"
    wait_for 20 all_clean
    result M2-14b INFO "after the start the plugin's startup cleanup left: $(leftovers)"
    sc stop "$PLUGIN_UNIT"
    check M2-14c "after start + stop no $LINK, guard or stale table is left" all_clean
    # d: the leftovers appear after the startup cleanup, so only ExecStopPost can remove them
    sc start "$PLUGIN_UNIT"
    sleep 3
    make_leftovers
    sc stop "$PLUGIN_UNIT"
    check M2-14d "leftovers made while the plugin idles are removed by ExecStopPost on stop" all_clean
    # e: another user's listener on a stale table's port does not keep the table
    as_user python3 -c 'import socket, time
s = [socket.create_server((a, 59998)) for a in ("127.0.0.1", "127.0.0.2")]
time.sleep(30)' &
    local lpid=$!
    wait_for 5 sh -c "ss -ltnH 'sport = :59998' | grep -q ."
    nft add table inet sshuttle-ipv4-59998
    "$LIBEXEC/nm-sshuttle" sweep 2> /dev/null
    check M2-14e "the sweep removes a stale table whose port another user listens on" \
        sh -c "! nft list tables | grep -q sshuttle-ipv4-59998"
    kill "$lpid" 2> /dev/null
    nft delete table inet sshuttle-ipv4-59998 2> /dev/null
    end_checks M2-14
}

s_selinux() {
    SCENARIO=M2-15
    note "M2-15 SELinux denials over the whole run"
    local n out other
    out=$(avc_since "$START_EPOCH" "$START_OFFSET")
    # Not the plugin's: Fedora's getty reading the issue file NetworkManager's
    # dispatcher writes for the console, seen in some runs
    other=$(grep -E 'comm="agetty".*tcontext=[^ ]*:NetworkManager_dispatcher_console_var_run_t:' <<< "$out")
    if [ -n "$other" ]; then
        result M2-15i INFO "ignored, not the plugin's: $(grep -c . <<< "$other") denial(s) of agetty reading NetworkManager's console issue file"
        out=$(grep -v -F -x -f <(echo "$other") <<< "$out")
    fi
    n=$(grep -c . <<< "$out")
    if [ -z "$out" ]; then
        result M2-15 PASS "no SELinux denial records during the run"
    else
        result M2-15 FAIL "$n SELinux denial records during the run (raw/avc.txt)"
        echo "$out" > "$REPORT/raw/avc.txt"
        # On the console too: a CI run's log may be all there is to read
        sed -E 's/^(type=[A-Z_]+) msg=audit\([^)]*\): pid=[0-9]+ uid=[0-9]+ auid=[0-9]+ ses=[0-9]+/\1/' <<< "$out" |
            sort | uniq -c | sort -rn | tee >(detail M2-15 "SELinux denials (deduplicated)") |
            head -n 20 | cut -c1-400 | sed 's/^/          /'
    fi
}

# --------------------------------------------------- M2 completion scenarios
reconnected_quickly() {  # the last reconnect, as the plugin measured it, took at most $1 s
    local l
    journal_catch_up
    l=$(plog | grep -o 'reconnected after [0-9.]*' | tail -n 1 | awk '{print $3}')
    echo "the plugin measured ${l:-no reconnect} s"
    [ -n "$l" ] && awk -v l="$l" -v m="$1" 'BEGIN {exit !(l <= m)}'
}

s_kill_sshuttle() {
    begin M2-16 "kill -9 of sshuttle while up"
    connect_ok M2-16 "activation before the kill" || return
    local ifi
    ifi=$(link_ifindex)
    log_mark
    drop_tunnel
    # NM still shows the old activation until the plugin sees the unit stop
    wait_for 15 plog_has "tunnel dropped"
    recovery_checks M2-16r "$ifi"
    check M2-16a "plugin log: the tunnel unit stopped, then a reconnect" plog_show "tunnel dropped (the tunnel unit stopped)"
    end_checks M2-16
}

s_flush() {
    begin M2-17 "nft flush ruleset while up: the health check restores the guard"
    connect_ok M2-17 "activation before the flush" || return
    local ifi t0 v=""
    ifi=$(link_ifindex)
    t0=$(now_ms)
    nft flush ruleset
    if WAIT_POLL=0.2 wait_for 15 guard_present; then v=$(( $(now_ms) - t0 )); fi
    check_le M2-17a "the guard table is back after the flush (health runs every 10 s)" "$v" 11000 ms
    check M2-17b "plugin log: the guard table is gone; installing it again" plog_show "the guard table is gone"
    check M2-17c "plugin log: no sshuttle table with a listener, so a reconnect" plog_show "no sshuttle table with a listener"
    recovery_checks M2-17r "$ifi"
    if fw_running; then
        firewall-cmd --reload > /dev/null 2>&1
        result M2-17z INFO "firewalld reloaded after the flush: $(fw_running && echo running)"
    fi
    end_checks M2-17
}

s_link_loss_up() {
    begin M2-18 "link loss while activated: branch 4"
    connect_ok M2-18 "activation before the link loss" || return
    local t0 v=""
    t0=$(now_ms)
    ip link del "$LINK"
    if wait_for 10 con_gone; then v=$(( $(now_ms) - t0 )); fi
    check_le M2-18a "the VPN goes to disconnected after 'ip link del $LINK' while activated" "$v" 2300 ms
    sleep 1
    check M2-18b "plugin log: give-up branch 4" plog_show "giving up (branch 4"
    check M2-18c "resolved has no $LINK link and no $DNS_IP" resolved_no_vpn
    dns_snap
    check M2-18d "NM DnsManager has no VPN entry" dns_no_vpn_entry
    check M2-18e "NM DnsManager has no entry without an interface" dns_no_bare_entry
    check M2-18f "no $LINK, guard or tunnel unit left" wait_for 10 all_clean
    clear_leaked_dns
    end_checks M2-18
}

s_link_loss_preup() {
    begin M2-19 "link loss before the first 'activated' (pre-up)"
    local mon out
    # delete the link as soon as the plugin sends its first Ip4Config (whichever
    # slot the activation got)
    ( stdbuf -oL busctl --system --no-pager monitor --match "$SIGMATCH,member='Ip4Config'" 2> /dev/null |
        while read -r line; do
            case $line in *Member=Ip4Config*) for l in $(any_links); do ip link del "$l" 2> /dev/null; done; echo "deleted at $(date +%T.%N)"; break ;; esac
        done > "$VARDIR/preup.txt" ) &
    mon=$!
    sleep 0.5
    activate > /dev/null 2>&1
    out=$ACTIVATE_OUT
    kill "$mon" 2> /dev/null
    pkill -f "member='Ip4Config'" 2> /dev/null
    wait "$mon" 2> /dev/null
    result M2-19i INFO "link deleted: $(cat "$VARDIR/preup.txt" 2> /dev/null || echo never); nmcli said: $(tr -s '\n' ' ' <<< "$out" | cut -c1-160)"
    check M2-19a "the VPN is not active afterwards" wait_for 15 con_gone
    result M2-19b INFO "give-up: $(plog | grep -F 'giving up' | tail -n 1 | cut -d' ' -f6- | cut -c1-200)"
    check M2-19c "no $LINK, guard or tunnel unit left" wait_for 10 all_clean
    dns_snap
    check M2-19d "NM DnsManager has no VPN entry" dns_no_vpn_entry
    check M2-19e "NM DnsManager has no entry without an interface" dns_no_bare_entry
    clear_leaked_dns
    end_checks M2-19
}

s_managed_yes() {
    begin M2-20 "nmcli device set $LINK managed yes in a gap"
    connect_ok M2-20 "activation before the gap" || return
    local ifi
    ifi=$(link_ifindex)
    gap_begin || { result M2-20g FAIL "no gap (state '$(con_state)')"; return; }
    sleep 2
    nmcli device set "$LINK" managed yes > "$REPORT/raw/managed-yes.txt" 2>&1
    result M2-20i INFO "nmcli device set $LINK managed yes said: '$(tr -s '\n' ' ' < "$REPORT/raw/managed-yes.txt")'"
    sleep 3
    result M2-20j INFO "3 s later: NM shows '$(con_state)', $LINK device state $(dev_state); bounced: $(plog | grep -c 'bouncing it back')"
    check M2-20a "guard still present and traffic still refused" guard_and_refused
    gap_end
    INFO_ONLY="y g" recovery_checks M2-20r "$ifi"
    end_checks M2-20
}

# stop_in_gap ID : systemctl stop of the plugin unit in a gap, and what must follow
stop_in_gap() {
    local id=$1 seq stop_pid
    gap_begin || { result "$id" FAIL "no gap (state '$(con_state)')"; return 1; }
    sleep 2
    signals_start "$REPORT/raw/signals-$id.txt"
    ( sc stop "$PLUGIN_UNIT"; echo $? > "$VARDIR/stop.rc" ) &
    stop_pid=$!
    watch_teardown 20
    wait "$stop_pid"
    signals_stop
    seq=$(signal_sequence "$REPORT/raw/signals-$id.txt")
    check "${id}a" "signals: Config, then Failure, then STOPPED" signals_in_order "$seq"
    check_le "${id}b" "the VPN goes down after the stop" "$T_CON" 3000 ms
    check_le "${id}c" "$LINK is removed" "$T_LINK" 2500 ms
    check_le "${id}d" "the guard is removed" "$T_GUARD" 3000 ms
    check_le "${id}f" "the service has exited" "$T_UNIT" 5500 ms
    check "${id}g" "systemctl stop succeeded" stop_result_ok
    check "${id}j" "nothing left: no link, guard, tunnel unit" all_clean
    dns_snap
    check "${id}k" "NM DnsManager has no VPN entry" dns_no_vpn_entry
    check "${id}l" "resolved has no $DNS_IP" resolved_no_vpn
}

write_override() {
    printf '[keyfile]\nunmanaged-devices=interface-name:nmtest-nosuch\n' | write_file "$NM_OVERRIDE"
    touch "$VARDIR/override-written"
}
remove_override() { rm -f "$NM_OVERRIDE" "$VARDIR/override-written"; }

s_stop_override() {
    begin M2-21 "service stop in a gap with the user override in place"
    write_override
    nmcli general reload conf
    sleep 1
    if connect_ok M2-21 "activation with a managed $LINK"; then
        result M2-21i INFO "$LINK device state $(dev_state)"
        stop_in_gap M2-21
    fi
    reset_state
    remove_override
    nmcli general reload conf
    sleep 1
    clear_leaked_dns
    end_checks M2-21
}

s_reload_conf() {
    begin M2-22 "nmcli general reload conf under an active VPN"
    connect_ok M2-22 "activation before the reloads" || return
    local ifi
    ifi=$(link_ifindex)
    write_override
    nmcli general reload conf
    sleep 2
    result M2-22i INFO "with the override reloaded: NM shows '$(con_state)', $LINK device state $(dev_state)"
    check M2-22a "the VPN stays activated through the reload" con_is activated
    check M2-22b "traffic still flows" traffic_ok
    remove_override
    nmcli general reload conf
    sleep 2
    result M2-22j INFO "with the override removed and reloaded: NM shows '$(con_state)', $LINK device state $(dev_state)"
    check M2-22c "the VPN stays activated through the second reload" con_is activated
    INFO_ONLY="y g" dns_checks M2-22d
    INFO_ONLY="y g" reconnect_cycle M2-22e 4
    end_checks M2-22
}

s_roam() {
    begin M2-23 "roaming: NM's primary connection changes to another uplink and back"
    lanb_setup || { result M2-23 SKIP "cannot create the second uplink"; return; }
    connect_ok M2-23 "activation on $UPLINK" || { lanb_teardown; return; }
    local ifi
    ifi=$(link_ifindex)
    log_mark
    if nmcli --wait 30 connection up "$LANB_CON" >> "$REPORT/raw/roam.txt" 2>&1; then
        result M2-23i INFO "second uplink up; primary connection: $(primary_con); NM: $(nm_general)"
        check M2-23a "plugin log: the network changed" wait_for 10 plog_has "the network changed"
        recovery_checks M2-23b "$ifi"
        check M2-23c "the reconnect took at most 10 s (at once, not after a backoff)" reconnected_quickly 10
        log_mark
        nmcli connection down "$LANB_CON" >> "$REPORT/raw/roam.txt" 2>&1
        result M2-23j INFO "back on $UPLINK; primary connection: $(primary_con)"
        check M2-23d "plugin log: the network changed again" wait_for 10 plog_has "the network changed"
        recovery_checks M2-23e "$ifi"
        check M2-23f "the reconnect took at most 10 s" reconnected_quickly 10
    else
        result M2-23 FAIL "cannot activate $LANB_CON: $(tail -n 2 "$REPORT/raw/roam.txt" | tr -s '\n' ' ')"
    fi
    lanb_teardown
    end_checks M2-23
}

s_no_default_route() {
    begin M2-24 "the uplink without a default route"
    connect_ok M2-24 "activation before the default route goes" || return
    local ifi
    ifi=$(link_ifindex)
    log_mark
    nmcli device modify "$UPLINK" ipv4.never-default yes >> "$REPORT/raw/no-default.txt" 2>&1
    touch "$VARDIR/never-default"
    sleep 3
    result M2-24i INFO "no default route: '$(ip -4 route show default | tr -s '\n' ' ')'; NM state,connectivity: $(nm_general)"
    check M2-24a "the plugin does not count it as offline" plog_has_not "NetworkManager is offline"
    check M2-24b "the VPN is activated (after a reconnect if the plugin saw a network change)" \
        wait_for 30 con_is activated
    INFO_ONLY="b" dns_checks M2-24c
    # No route to the subnets either: connect() fails before sshuttle's
    # redirect can apply, so traffic through the tunnel is INFO here
    INFO_ONLY="b w" reconnect_cycle M2-24d 4
    nmcli device modify "$UPLINK" ipv4.never-default no >> "$REPORT/raw/no-default.txt" 2>&1
    rm -f "$VARDIR/never-default"
    sleep 3
    result M2-24j INFO "default route back: '$(ip -4 route show default | tr -s '\n' ' ')'"
    end_checks M2-24
}

lock_gap() {  # lock_gap : lock the session, break the key, drop the tunnel
    set_locked true > /dev/null || return 1
    keys_off
    drop_tunnel
    wait_for 15 con_is activating || return 1
    wait_for 20 plog_has "waiting for the unlock"
}
s_lock() {
    begin M2-25 "lock screen: a reconnect that needs the user waits for the unlock and retries once"
    if ! lock_session_start; then
        result M2-25 SKIP "cannot open a logind session for $USER_NAME"
        return
    fi
    result M2-25i INFO "session $LOCK_SESSION of $USER_NAME"
    local ifi n
    if connect_ok M2-25 "activation before the lock"; then
        ifi=$(link_ifindex)
        # a: locked, key broken, then the key works again and the screen is unlocked
        if lock_gap; then
            result M2-25a PASS "plugin log: authentication failed while locked; waiting for the unlock"
            n=$(attempts_logged)
            sleep 12
            check M2-25b "no attempt while locked (attempt lines stay at $n)" test "$(attempts_logged)" = "$n"
            check M2-25c "still reconnecting: NM activating, guard present, $LINK kept" still_in_gap "$ifi"
            keys_on
            set_locked false > /dev/null
            check M2-25d "plugin log: unlocked; retrying once" wait_for 10 plog_has "unlocked; retrying once"
            recovery_checks M2-25r "$ifi"
        else
            result M2-25a FAIL "the plugin did not wait for the unlock (state '$(con_state)')"
            plog | tail -n 20 | detail M2-25a "plugin journal"
            keys_on
            set_locked false > /dev/null
        fi
        # b: locked, key broken, unlocked with the key still broken: login failed
        log_mark
        if con_is activated && lock_gap; then
            set_locked false > /dev/null
            check M2-25e "after the unlock the one retry fails and the VPN goes down" wait_for 30 con_gone
            check M2-25f "plugin log: the retry after the unlock failed" plog_show "the retry after the unlock failed"
        else
            result M2-25e FAIL "no locked gap for the second case (state '$(con_state)')"
        fi
        keys_on
        set_locked false > /dev/null
    fi
    # c: unlocked, key broken: login failed at once, no waiting
    reset_state
    log_mark
    if connect_ok M2-25g "activation for the unlocked case"; then
        keys_off
        drop_tunnel
        check M2-25h "unlocked: the VPN goes down on the authentication failure" wait_for 30 con_gone
        check M2-25j "plugin log: reconnect failed on authentication" plog_show "reconnect failed on authentication"
        check M2-25k "plugin log: no wait for an unlock" plog_has_not "waiting for the unlock"
        keys_on
    fi
    lock_session_stop
    end_checks M2-25
}

s_park() {
    begin M2-26 "candidate: park NM in ip-config-get during a gap"
    write_file "$PARK_DROPIN" <<< $'[Service]\nEnvironment=NM_SSHUTTLE_PARK_GAP=1'
    sc daemon-reload
    local ifi oldpid
    if connect_ok M2-26 "activation with the candidate on"; then
        check M2-26a "plugin log: the candidate is on" plog_show "candidate: parking"
        ifi=$(link_ifindex)
        if gap_begin; then
            sleep 4
            check M2-26b "plugin log: parking NM in ip-config-get" plog_show "parking NM in ip-config-get"
            check M2-26c "4 s into the parked gap NM still shows activating (no early flip)" con_is activating
            result M2-26i INFO "plugin signals so far: $(plog | grep -cE 'Config:') Config lines; bounces: $(plog | grep -c 'bouncing it back')"
            gap_checks M2-26g "$ifi"
            gap_end
            recovery_checks M2-26r "$ifi"
            # a kill in a parked gap: NM should now fail the VPN from ip-config-get and drop its DNS
            if gap_begin; then
                sleep 3
                oldpid=$(plugin_pid)
                kill -9 "$oldpid"
                check M2-26k "the VPN goes down after the kill" wait_for 15 con_gone
                check M2-26l "$LINK, guard and tunnel unit are gone" wait_for 15 all_clean
                dns_snap
                check M2-26m "NM DnsManager has no VPN entry after a kill in a parked gap (the candidate's claim; M2-09 leaks it)" dns_no_vpn_entry
                gap_end
            fi
        fi
    fi
    reset_state
    rm -f "$PARK_DROPIN"
    sc daemon-reload
    clear_leaked_dns
    end_checks M2-26
}

probe_answered_twice() { [ "$(plog | grep -c 'answers through the tunnel')" -ge 2 ]; }
s_probe() {
    begin M2-27 "probe: forwarding dead while sshuttle and ssh live"
    banner_start || { result M2-27 SKIP "cannot start the banner server"; return; }
    create_profile "$CON" "probe = $WEB_IP:$BANNER_PORT"
    local ifi pids=()
    if connect_ok M2-27 "activation with probe = $WEB_IP:$BANNER_PORT"; then
        ifi=$(link_ifindex)
        check M2-27a "plugin log: the probe answers through the tunnel" wait_for 25 plog_has "answers through the tunnel"
        read -ra pids <<< "$(remote_server_pids | xargs)"
        if [ "${#pids[@]}" -gt 0 ]; then
            result M2-27i INFO "stopping the sshuttle server on the jump host (pids ${pids[*]}); ssh and sshd keep the session"
            kill -STOP "${pids[@]}"
            check M2-27b "plugin log: the probe failed twice, so a reconnect" wait_for 45 plog_has "the probe failed 2 times in a row"
            recovery_checks M2-27r "$ifi"
            check M2-27c "the probe answers again after the reconnect" wait_for 25 probe_answered_twice
            kill -KILL "${pids[@]}" 2> /dev/null
        else
            result M2-27b FAIL "cannot find the sshuttle server's processes in netns jump"
        fi
    fi
    reset_state
    create_profile "$CON"
    banner_stop
    end_checks M2-27
}

# A local resolver on h-j that knows jump.corp.test; ~. makes it the default, so
# nmss0's ~corp.test wins for the name once the tunnel's DNS is up.
JUMP_NAME=jump.corp.test
jump_name_setup() {
    systemd-run --quiet --collect --unit=nmtest-dns-local \
        -p NetworkNamespacePath=/run/netns/jump \
        /usr/bin/python3 "$REPO/lab/dns_server.py" $JUMP_IP "$JUMP_NAME=$JUMP_IP" || return 1
    wait_for 10 sh -c "ss -N jump -lunH 'sport = :53' | grep -q ."
    rv dns h-j $JUMP_IP && rv domain h-j '~.' && rv default-route h-j yes || return 1
    [ -e "$HOME_U/.ssh/config" ] && return 1
    touch "$VARDIR/ssh-config-written"
    as_user sh -c "umask 077; printf 'Host nmtest-pj\n    HostName %s\n    ProxyJump %s\n' \
        $JUMP_IP $JUMP_NAME > $HOME_U/.ssh/config"
    sed -n "s/^$JUMP_IP /$JUMP_NAME /p" "$VARDIR/known_hosts.tmp" >> "$HOME_U/.ssh/known_hosts"
    raw jump-name rv query "$JUMP_NAME"
}
jump_name_teardown() {
    sc stop nmtest-dns-local > /dev/null 2>&1
    rv revert h-j > /dev/null 2>&1
    if [ -f "$VARDIR/ssh-config-written" ]; then
        rm -f "$HOME_U/.ssh/config" "$VARDIR/ssh-config-written"
        as_user ssh-keygen -R "$JUMP_NAME" > /dev/null 2>&1
    fi
    return 0
}
pin_count() { journal_catch_up; grep -c -F "the first hop's name does not resolve" "$JOURNAL_LOG"; }
pinned_jump() { grep -A2 "^Host $JUMP_NAME\$" "/run/nm-sshuttle-ssh/ssh_config-$SLOT" | grep -q "HostName $JUMP_IP"; }

s_jump_name() {
    begin M2-29 "a ProxyJump host named inside the tunnelled DNS domain"
    jump_name_setup || { result M2-29 SKIP "cannot set up the local resolver and ssh config"; jump_name_teardown; return; }
    REMOTE=nmtest-pj create_profile "$CON"
    local ifi pins
    if connect_ok M2-29 "activation through ProxyJump $JUMP_NAME, looked up on the local network"; then
        ifi=$(link_ifindex)
        check M2-29a "plugin log: the first hop is $JUMP_IP" plog_show "first hop $JUMP_IP"
        pins=$(pin_count)
        drop_tunnel
        wait_for 15 plog_has "tunnel dropped"
        recovery_checks M2-29r "$ifi"
        check M2-29b "tunnel log: $JUMP_NAME did not resolve, so ssh got the address from connect" \
            test "$(pin_count)" -gt "$pins"
        check M2-29c "the generated ssh config pins $JUMP_NAME to $JUMP_IP" pinned_jump
    fi
    reset_state
    jump_name_teardown
    create_profile "$CON"
    end_checks M2-29
}

# --------------------------------------------------------- IPv6 (docs/later/ipv6.md)
refused6_fast() {
    local t0 out rc
    t0=$(now_ms)
    out=$(curl -g -s -m 5 -o /dev/null -w '%{http_code}' --noproxy '*' "http://[$WEB_IP6]:8080/" 2>&1)
    rc=$?
    echo "curl exit $rc, http '$out', $(( $(now_ms) - t0 )) ms"
    [ "$rc" = 7 ] && [ $(( $(now_ms) - t0 )) -lt 3000 ]
}
guard_has_v6() { nft list set inet "$GUARD_TABLE" subnets6 | grep -F '2001:db8:99::/64'; }
sshuttle_has_v6() { sshuttle_tables; sshuttle_tables | grep -q ipv6; }
network_changed_logged() { plog_has "the network changed"; }
con6_is() { [ "$(con_state "$CON6")" = "$1" ]; }
con6_gone() { [ -z "$(con_state "$CON6")" ]; }

s_ipv6() {
    begin V6-01 "IPv6: a subnet through the tunnel, fail closed in a gap"
    [ "$HAVE_IPV6" = yes ] || { result V6-01 SKIP "the kernel has no IPv6"; return; }
    SUBNETS="10.99.0.0/24 2001:db8:99::/64" create_profile "$CON6"
    connect_ok V6-01 "activation of a profile with an IPv6 subnet" "$CON6" || return
    local ifi
    ifi=$(link_ifindex)
    check V6-01a "the guard's subnets6 set holds 2001:db8:99::/64" guard_has_v6
    check V6-01b "sshuttle runs without --disable-ipv6 and has an IPv6 table" sshuttle_has_v6
    check V6-01c "IPv6 traffic to $WEB_IP6 goes through the tunnel" http_ok "http://[$WEB_IP6]:8080/"
    check V6-01d "... and arrives from the jump host, not from $HOST_IP6" web6_from 2001:db8:99::1
    check V6-01e "IPv4 traffic still goes through the tunnel" traffic_ok
    block_jump
    drop_tunnel
    wait_for 15 con6_is activating || { result V6-01f FAIL "no gap: state '$(con_state "$CON6")'"; gap_end; return; }
    check V6-01f "IPv6 traffic to the subnet is refused at once during the gap (fail closed)" refused6_fast
    check V6-01g "IPv4 likewise" refused_fast
    gap_end
    if WAIT_POLL=0.5 wait_for 150 con6_is activated; then
        result V6-01h PASS "activated again after the gap"
        sleep 1
        check V6-01i "IPv6 traffic flows through the tunnel again" http_ok "http://[$WEB_IP6]:8080/"
        check V6-01j "same ifindex ($ifi) after the reconnect" test "$(link_ifindex)" = "$ifi"
    else
        result V6-01h FAIL "not activated again within 150 s (state '$(con_state "$CON6")')"
    fi
    end_checks V6-01
}

s_ipv6_roam() {
    begin V6-02 "IPv6: roams that change only the uplink's IPv6 addresses"
    [ "$HAVE_IPV6" = yes ] || { result V6-02 SKIP "the kernel has no IPv6"; return; }
    lanb6_setup || { result V6-02 SKIP "cannot create the second uplink"; return; }
    if ! nmcli --wait 30 connection up "$LANB6_CON" >> "$REPORT/raw/roam6.txt" 2>&1; then
        result V6-02 FAIL "cannot activate $LANB6_CON"
        lanb6_teardown
        return
    fi
    result V6-02i INFO "primary connection: $(primary_con)"
    # The first hop by its IPv6 address: the IPv6 part of the network counts.
    REMOTE="$USER_NAME@$JUMP_IP6" create_profile "$CON6"
    connect_ok V6-02 "activation with the first hop $JUMP_IP6" "$CON6" || { lanb6_teardown; return; }
    check V6-02a "plugin log: the first hop is the IPv6 address" plog_show "first hop $JUMP_IP6"
    local ifi
    ifi=$(link_ifindex)
    log_mark
    lanb6_address 2001:db8:77::2/64 2001:db8:77::4d2f/64
    sleep 5
    check V6-02b "a second address in the same prefix (as a temporary one) is no network change" \
        plog_has_not "the network changed"
    check V6-02c "... and the VPN stays activated" con6_is activated
    log_mark
    lanb6_address 2001:db8:78::2/64
    check V6-02d "a new IPv6 prefix is a network change" wait_for 10 network_changed_logged
    if WAIT_POLL=0.5 wait_for 60 con6_is activated; then
        result V6-02e PASS "activated again after the prefix change"
        check V6-02f "the reconnect took at most 10 s (at once, not after a backoff)" reconnected_quickly 10
        check V6-02g "traffic flows through the tunnel again" http_ok "http://$WEB_IP:8080/"
    else
        result V6-02e FAIL "not activated again within 60 s (state '$(con_state "$CON6")')"
    fi
    # The same prefix change with an IPv4 first hop and IPv4 networks: no change.
    nmcli connection down "$CON6" > /dev/null 2>&1
    wait_for 30 con6_gone
    connect_ok V6-02k "activation of the IPv4 profile" || { lanb6_teardown; return; }
    log_mark
    lanb6_address 2001:db8:77::2/64
    sleep 5
    check V6-02l "a new IPv6 prefix is no network change for an IPv4 tunnel" \
        plog_has_not "the network changed"
    check V6-02m "... and the VPN stays activated" con_is activated
    lanb6_teardown
    end_checks V6-02
}

# ------------------------------------------------- tproxy (docs/later/tproxy.md)
udp_echo() { python3 "$REPO/lab/udp_echo.py" send $WEB_IP $UDP_PORT "$@"; }
udp_ok() { local out; out=$(udp_echo "$1"); echo "$out"; [ "$out" = "ECHO $1 from 10.99.0.1" ]; }
udp_refused() { local out; out=$(udp_echo gap); echo "$out"; [ "$out" = "ERROR Operation not permitted" ]; }
contp_is() { [ "$(con_state "$CONTP")" = "$1" ]; }
contp_gone() { [ -z "$(con_state "$CONTP")" ]; }
has_mark_chain() { mangle_chains; mangle_chains | grep -q '^sshuttle-m-'; }
routing_present() { tproxy_rule && tproxy_route; }
routing_gone() { [ -z "$(tproxy_rule)$(tproxy_route)" ]; }
tproxy_clean() {
    echo "chains: '$(mangle_chains | xargs)'; rule: '$(tproxy_rule)'; route: '$(tproxy_route)'"
    [ -z "$(mangle_chains)" ] && routing_gone
}
have_iptables() { command -v iptables > /dev/null && command -v ip6tables > /dev/null; }
# Fedora's sshuttle pulls in iptables-legacy, and which backend iptables uses
# then depends on the install order
iptables_legacy() { iptables -V 2> /dev/null | grep -q legacy; }
# tp_recovered ID IFINDEX : activated again, same link, UDP through the tunnel
tp_recovered() {
    if WAIT_POLL=0.5 wait_for 150 contp_is activated; then
        result "$1" PASS "activated again ($((WAITED_MS / 1000)) s)"
    else
        result "$1" FAIL "not activated again within 150 s (state '$(con_state "$CONTP")')"
        plog | tail -n 30 | detail "$1" "plugin journal"
        return 1
    fi
    sleep 1
    check "$1-i" "same ifindex ($2) after the reconnect" test "$(link_ifindex)" = "$2"
    check "$1-c" "sshuttle's mark chain is back" has_mark_chain
    check "$1-u" "UDP flows through the tunnel again" udp_ok again
}

s_tproxy() {
    begin TP-01 "method=tproxy: UDP and TCP through the tunnel, fail closed in a gap"
    have_iptables || { result TP-01 SKIP "iptables and ip6tables are not installed"; return; }
    create_profile "$CONTP" "method = tproxy"
    connect_ok TP-01 "activation of a method=tproxy profile" "$CONTP" || return
    local ifi
    ifi=$(link_ifindex)
    result TP-01i INFO "$(iptables -V 2>&1)"
    check TP-01a "sshuttle has a mark chain in iptables' mangle table" has_mark_chain
    check TP-01b "the plugin's rule and local route are in place" routing_present
    check TP-01c "UDP echo to $WEB_IP:$UDP_PORT answers, from the jump host" udp_ok hello
    check TP-01d "TCP, by address and by a split DNS name" traffic_ok
    check TP-01e "a fresh split name resolves through the tunnel (resolved, IP_UNICAST_IF)" split_lookup_ok
    block_jump
    drop_tunnel
    wait_for 15 contp_is activating || { result TP-01f FAIL "no gap: state '$(con_state "$CONTP")'"; gap_end; return; }
    check TP-01f "UDP to the subnet is refused at once during the gap (fail closed)" udp_refused
    check TP-01g "TCP likewise" refused_fast
    check TP-01h "the rule and route stay during the gap" routing_present
    gap_end
    tp_recovered TP-01r "$ifi"
    nmcli connection down "$CONTP" > /dev/null 2>&1
    wait_for 30 contp_gone
    check TP-01z "after the disconnect: no mark chain, rule or route left" wait_for 10 tproxy_clean
    end_checks TP-01
}

s_tproxy_health() {
    begin TP-02 "method=tproxy: kill sshuttle and nft flush ruleset while up"
    have_iptables || { result TP-02 SKIP "iptables and ip6tables are not installed"; return; }
    create_profile "$CONTP" "method = tproxy"
    connect_ok TP-02 "activation of a method=tproxy profile" "$CONTP" || return
    local ifi t0 v=""
    ifi=$(link_ifindex)
    drop_tunnel
    wait_for 15 plog_has "tunnel dropped"
    check TP-02a "plugin log: the tunnel unit stopped, then a reconnect" plog_show "tunnel dropped (the tunnel unit stopped)"
    tp_recovered TP-02b "$ifi" || { end_checks TP-02; return; }
    log_mark
    t0=$(now_ms)
    nft flush ruleset
    if WAIT_POLL=0.2 wait_for 15 guard_present; then v=$(( $(now_ms) - t0 )); fi
    check_le TP-02c "the guard table is back after the flush" "$v" 11000 ms
    if iptables_legacy; then        # its chains are not in nft, so the flush leaves them
        check TP-02d "iptables-legacy: the flush left sshuttle's mark chain, so no reconnect" \
            has_mark_chain
    else
        wait_for 15 plog_has "no sshuttle mark chain with a listener"
        check TP-02d "plugin log: no sshuttle mark chain with a listener, so a reconnect" \
            plog_show "no sshuttle mark chain with a listener"
    fi
    check TP-02e "the flush left the rule and route alone" routing_present
    tp_recovered TP-02f "$ifi"
    if fw_running; then
        firewall-cmd --reload > /dev/null 2>&1
        result TP-02z INFO "firewalld reloaded after the flush: $(fw_running && echo running)"
    fi
    end_checks TP-02
}

s_tproxy_nm() {
    begin TP-03 "method=tproxy: NM leaves the policy routing alone"
    have_iptables || { result TP-03 SKIP "iptables and ip6tables are not installed"; return; }
    create_profile "$CONTP" "method = tproxy"
    connect_ok TP-03 "activation of a method=tproxy profile" "$CONTP" || return
    local ifi
    ifi=$(link_ifindex)
    nmcli general reload > /dev/null 2>&1
    sleep 3
    check TP-03a "after 'nmcli general reload' the rule and route are still there" routing_present
    check TP-03b "... and UDP flows" udp_ok reload
    if lanb_setup && nmcli --wait 30 connection up "$LANB_CON" >> "$REPORT/raw/roam.txt" 2>&1; then
        check TP-03c "plugin log: the network changed" wait_for 10 plog_has "the network changed"
        tp_recovered TP-03d "$ifi"
        check TP-03e "after the roam the rule and route are still there" routing_present
    else
        result TP-03c SKIP "cannot activate the second uplink"
    fi
    lanb_teardown
    end_checks TP-03
}

s_suspend() {
    begin M2-28 "suspend and resume"
    if [ "$SUSPEND" != 1 ]; then
        result M2-28 SKIP "runs only with --suspend: the VM must wake from its RTC, and a VM that does not wake ends the run"
        return
    fi
    local can ifi t0
    can=$(bc call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager CanSuspend 2>&1)
    if [ "$can" != 's "yes"' ]; then
        result M2-28 SKIP "logind CanSuspend says $can"
        return
    fi
    connect_ok M2-28 "activation before the suspend" || return
    ifi=$(link_ifindex)
    sleep 1
    check M2-28a "the plugin holds a sleep delay inhibitor" inhibitor_held
    log_mark
    rtcwake -m no -s 30 > "$REPORT/raw/rtcwake.txt" 2>&1
    t0=$(date +%s)
    sc suspend
    sleep 5
    WAIT_POLL=1 wait_for 120 plog_has "resumed"
    result M2-28i INFO "suspend and resume took $(( $(date +%s) - t0 )) s (RTC alarm at 30 s)"
    check M2-28b "plugin log: suspending, the tunnel stopped before sleep" plog_show "tunnel dropped (the machine is suspending)"
    check M2-28c "plugin log: resumed" plog_show "resumed"
    recovery_checks M2-28r "$ifi"
    check M2-28d "the plugin holds the inhibitor again after the resume" inhibitor_held
    end_checks M2-28
}

# ------------------------------------------------------------ several tunnels
# docs/later/multiple-tunnels.md. corp covers 10.99.0.0/24 except the lab's
# 10.99.0.16/28 and its DNS server; the lab has its own DNS server and zone.
# Both run over the jump host (MT-01), or the lab over corp, through an sshd
# inside the corporate network (MT-02).
mt_setup() {
    local a
    for a in $LAB_WEB $LAB_DNS $LAB_JUMP; do ip -n internal addr add "$a/24" dev i-j 2> /dev/null; done
    sc is-active --quiet nmtest-dns2 || systemd-run --quiet --collect --unit=nmtest-dns2 -p SyslogIdentifier=nmtest-dns2 \
        -p NetworkNamespacePath=/run/netns/internal \
        /usr/bin/python3 "$REPO/lab/dns_server.py" $LAB_DNS lab.test $LAB_WEB
    sc is-active --quiet nmtest-web2 || systemd-run --quiet --collect --unit=nmtest-web2 \
        -p NetworkNamespacePath=/run/netns/internal \
        /usr/bin/python3 -m http.server --bind $LAB_WEB 8080 --directory "$VARDIR/www"
    if ! sc is-active --quiet nmtest-sshd2; then
        { grep -v '^ListenAddress' "$SSHD_DIR/sshd_config"; echo "ListenAddress $LAB_JUMP"; } \
            > "$SSHD_DIR/sshd2_config"
        restorecon -RF "$SSHD_DIR" 2> /dev/null
        systemd-run --quiet --collect --unit=nmtest-sshd2 \
            -p NetworkNamespacePath=/run/netns/internal /usr/sbin/sshd -D -e -f "$SSHD_DIR/sshd2_config"
        grep -q "^$LAB_JUMP " "$HOME_U/.ssh/known_hosts" ||
            echo "$LAB_JUMP $(cut -d' ' -f1,2 "$SSHD_DIR/host_ed25519.pub")" >> "$HOME_U/.ssh/known_hosts"
    fi
    wait_for 10 mt_services_up
}
mt_services_up() {
    ss -N internal -ltnH "src $LAB_WEB and sport = :8080" | grep -q . &&
        ss -N internal -lunH "src $LAB_DNS and sport = :53" | grep -q . &&
        ss -N internal -ltnH "src $LAB_JUMP and sport = :22" | grep -q .
}
mt_profiles() {  # mt_profiles LAB-REMOTE : corp without the lab's part, the lab, and one that overlaps corp
    create_profile "$CON" "exclude = 10.99.0.16/28 $LAB_DNS/32"
    REMOTE=$1 SUBNETS=10.99.0.16/28 DNS_SERVERS=$LAB_DNS DOMAINS=~lab.test create_profile "$CONL"
    SUBNETS=10.99.0.10/32 DOMAINS=~other.test create_profile "$CONX"
}
mt_teardown() {
    reset_state
    sc stop nmtest-sshd2 nmtest-dns2 nmtest-web2 > /dev/null 2>&1
    nmcli connection delete "$CONL" "$CONX" > /dev/null 2>&1
    create_profile "$CON"
}
con_uuid() { nmcli -g connection.uuid connection show "$1" 2> /dev/null; }
slot_of() {  # slot_of CONNECTION : the slot whose claim is the connection's
    local u f
    u=$(con_uuid "$1")
    for f in "$STATE_DIR"/[0-7]/claim.json; do
        [ -e "$f" ] && grep -q "\"uuid\": \"$u\"" "$f" && { basename "$(dirname "$f")"; return 0; }
    done
    return 1
}
con_state_is() { [ "$(con_state "$1")" = "$2" ]; }
con_down() { [ -z "$(con_state "$1")" ]; }
both_http() { http_ok "http://$WEB_IP:8080/" && http_ok "http://$LAB_WEB:8080/"; }
two_links() { ip -4 -o addr show dev "nmss$1" | grep -F 192.0.0.8/32 && ip -4 -o addr show dev "nmss$2" | grep -F 192.0.0.8/32; }
two_guards() { nft list table inet "nm-sshuttle-guard-$1" > /dev/null && nft list table inet "nm-sshuttle-guard-$2" > /dev/null; }
mt_resolved() {  # mt_resolved LINK SERVER ZONE : the link carries the server and the routing domain
    local dns dom
    dns=$(rv dns "$1" 2>&1)
    dom=$(rv domain "$1" 2>&1)
    printf '%s\n%s\n' "$dns" "$dom"
    contains "$dns" "$2" && contains "$dom" "~$3"
}
mt_logged() { servers_log "$1" | grep -q -F -- ": $2 type"; }
mt_lookup() {  # mt_lookup ZONE ADDRESS UNIT : a fresh name resolves, and that zone's server saw it
    local n="u$RANDOM$RANDOM.$1" out
    out=$(rv query --cache=no --legend=no "$n" 2>&1)
    echo "$out"
    contains "$out" "$2" || return 1
    wait_for 10 mt_logged "$3" "$n" || { echo "$3 never logged $n"; return 1; }
}
two_vpn_entries() {
    echo "$DNS_SNAP"
    [ "$(grep -c '^vpn=1 ' <<< "$DNS_SNAP")" = 2 ] &&
        grep -q "^vpn=1 iface=nmss$1 ns=$DNS_IP" <<< "$DNS_SNAP" &&
        grep -q "^vpn=1 iface=nmss$2 ns=$LAB_DNS" <<< "$DNS_SNAP"
}
tunnel_pid() { sc show -p MainPID --value "nm-sshuttle-tunnel@$1.service" 2> /dev/null; }
corp_untouched() {  # corp_untouched SLOT SSHUTTLE-PID : still activated, same sshuttle, link and guard
    echo "corp: '$(con_state "$CON")', sshuttle pid $(tunnel_pid "$1") (was $2)"
    con_state_is "$CON" activated && [ "$(tunnel_pid "$1")" = "$2" ] &&
        [ -e "/sys/class/net/nmss$1" ] && nft list table inet "nm-sshuttle-guard-$1" > /dev/null
}
slot_clean() {  # slot_clean N : the slot's link, guard and tunnel unit are gone
    local t
    t=$(sc is-active "nm-sshuttle-tunnel@$1.service" 2> /dev/null)
    echo "nmss$1: $([ -e "/sys/class/net/nmss$1" ] && echo present || echo gone); tunnel: $t"
    [ ! -e "/sys/class/net/nmss$1" ] && ! nft list table inet "nm-sshuttle-guard-$1" > /dev/null 2>&1 &&
        case $t in active|activating|deactivating) false ;; *) true ;; esac
}
check_names_conflict() {  # nm-sshuttle check, as the user, reports the overlap
    local out
    out=$(as_user nm-sshuttle check "$CONX" 2>&1)
    echo "$out"
    contains "$out" "FAIL subnets overlap with the active profile $CON"
}
two_running() { echo "running: '$(running_slots | xargs)'"; [ "$(running_slots | wc -l)" = 2 ]; }

s_multi() {
    begin MT-01 "several tunnels: two at once, one reconnects, a third overlaps, a kill, an NM restart"
    mt_setup || { result MT-01 FAIL "cannot start the lab's DNS, web and sshd"; mt_teardown; return; }
    mt_profiles "$USER_NAME@$JUMP_IP"
    local sc_="" sl="" pidc ifl t0
    if connect_ok MT-01 "corp activates" && connect_ok MT-01a "the lab activates next to it" "$CONL"; then
        sc_=$(slot_of "$CON")
        sl=$(slot_of "$CONL")
    fi
    if [ -z "$sc_" ] || [ -z "$sl" ] || [ "$sc_" = "$sl" ]; then
        result MT-01b FAIL "two instances in two slots (corp '$sc_', lab '$sl')"
        mt_teardown
        return
    fi
    result MT-01b PASS "two instances in two slots (corp $sc_, lab $sl)"
    check MT-01c "both links carry 192.0.0.8/32" two_links "$sc_" "$sl"
    check MT-01d "both guards exist" two_guards "$sc_" "$sl"
    check MT-01e "traffic to corp ($WEB_IP) and to the lab ($LAB_WEB) goes through the tunnels" both_http
    check MT-01f "resolved: nmss$sc_ has $DNS_IP and ~corp.test" mt_resolved "nmss$sc_" "$DNS_IP" corp.test
    check MT-01g "resolved: nmss$sl has $LAB_DNS and ~lab.test" mt_resolved "nmss$sl" "$LAB_DNS" lab.test
    check MT-01h "a corp name resolves through corp's server" mt_lookup corp.test "$WEB_IP" nmtest-dns
    check MT-01i "a lab name resolves through the lab's server" mt_lookup lab.test "$LAB_WEB" nmtest-dns2
    dns_snap
    check MT-01j "NM DnsManager: one VPN entry on each link" two_vpn_entries "$sc_" "$sl"

    # one tunnel reconnects: the jump host still accepts it, so it is back at once
    pidc=$(tunnel_pid "$sc_")
    use_slot "$sl"
    ifl=$(link_ifindex)
    drop_tunnel
    wait_for 15 con_state_is "$CONL" activating
    check MT-01k "the lab reconnects after its tunnel dies" wait_for 60 con_state_is "$CONL" activated
    check MT-01l "corp was untouched (same sshuttle, link and guard)" corp_untouched "$sc_" "$pidc"
    check MT-01m "the lab keeps its link (ifindex $ifl)" test "$(link_ifindex)" = "$ifl"
    check MT-01n "traffic through both again" wait_for 10 both_http

    # a third profile that overlaps corp is refused, with corp's name
    activate "$CONX" > /dev/null 2>&1
    check MT-01o "an overlapping third profile is refused" con_down "$CONX"
    check MT-01p "plugin log names the profile it overlaps" plog_show "subnets overlap with the active profile $CON: 10.99.0.10/32"
    check MT-01q "nm-sshuttle check reports the overlap" check_names_conflict
    check MT-01r "the refused instance exits; the two others run on" wait_for 15 two_running
    check MT-01s "traffic through both still" both_http

    # kill -9 of one instance: its ExecStopPost cleans only its slot
    use_slot "$sl"
    kill -9 "$(plugin_pid)"
    check MT-01t "the lab goes down after the kill" wait_for 15 con_down "$CONL"
    check MT-01u "the lab's slot is cleaned (nmss$sl, its guard, its tunnel unit)" wait_for 15 slot_clean "$sl"
    check MT-01v "corp was untouched" corp_untouched "$sc_" "$pidc"
    check MT-01w "traffic to corp still flows" http_ok "http://$WEB_IP:8080/"

    # an NM restart with two tunnels up: both tear down
    if connect_ok MT-01y "the lab activates again" "$CONL"; then
        t0=$(now_ms)
        sc restart --no-block NetworkManager
        check MT-01z "after an NM restart nothing is left in any slot" wait_for 20 all_clean
        result MT-01z-i INFO "torn down $(( $(now_ms) - t0 )) ms after the restart was requested"
        nm-online -s -q -t 60
        sleep 2
        dns_snap
        check MT-01z-d "NM DnsManager has no VPN entry" dns_no_vpn_entry
    fi
    clear_leaked_dns
    end_checks MT-01
    mt_teardown
}

mt_lab_diag() {  # mt_lab_diag SLOT : why the lab stayed up during corp's gap
    local pid
    pid=$(sc show -p MainPID --value "nm-sshuttle@$1.service" 2> /dev/null)
    result MT-02e-i INFO "the lab's tunnel unit: $(sc is-active "nm-sshuttle-tunnel@$1.service");" \
        "connections to $LAB_JUMP:22: $(ss -tnpH "dst $LAB_JUMP" | xargs | cut -c1-300)"
    sc status -l "nm-sshuttle-tunnel@$1.service" 2>&1 | sed -n '/CGroup:/,$p' | head -n 8 |
        cut -c1-180 | sed 's/^/          p /'
    ss -tanpH 2>&1 | grep -E '"(ssh|sshuttle|python3?)"' | cut -c1-180 | sed 's/^/          s /'
    plog | grep -F "[$pid]:" | grep -E 'invisible|active connection' | head -n 3 | sed 's/^/          l /'
    plog | grep -F "[$pid]:" | tail -n 15 | sed 's/^/          l /'
}

s_nested() {
    begin MT-02 "several tunnels: the lab over corp; a corp gap takes the lab down, and it is back at once after corp"
    mt_setup || { result MT-02 FAIL "cannot start the lab's DNS, web and sshd"; mt_teardown; return; }
    mt_profiles "$USER_NAME@$LAB_JUMP"
    local sc_ sl_ t_corp t_lab
    connect_ok MT-02 "corp activates" || { mt_teardown; return; }
    sc_=$SLOT
    if ! connect_ok MT-02a "the lab activates, its ssh to $LAB_JUMP running over corp" "$CONL"; then
        mt_teardown
        return
    fi
    sl_=$SLOT
    check MT-02b "plugin log: the lab's first hop is $LAB_JUMP" plog_show "first hop $LAB_JUMP"
    check MT-02c "traffic to corp and to the lab" both_http
    check MT-02d "a lab name resolves through the lab's server" mt_lookup lab.test "$LAB_WEB" nmtest-dns2
    result MT-02i INFO "the lab's ssh to $LAB_JUMP: $(ss -tnpH "dst $LAB_JUMP" | xargs | cut -c1-200)"
    log_mark
    use_slot "$sc_"
    gap_begin || { result MT-02e FAIL "no corp gap (state '$(con_state)')"; mt_teardown; return; }
    check MT-02e "the lab drops with corp" wait_for 30 con_state_is "$CONL" activating
    con_state_is "$CONL" activated && mt_lab_diag "$sl_"
    sleep 10                        # the lab's backoff grows meanwhile
    gap_end
    if wait_for 60 con_is activated; then
        t_corp=$(now_ms)
        wait_for 60 con_state_is "$CONL" activated
        t_lab=$(( $(now_ms) - t_corp ))
        con_state_is "$CONL" activated || t_lab=""
        check_le MT-02f "the lab is activated again soon after corp" "$t_lab" 6000 ms
    else
        result MT-02f FAIL "corp did not come back within 60 s"
    fi
    check MT-02g "plugin log: another VPN was activated, so the lab tried at once" plog_show "another VPN was activated"
    check MT-02h "traffic to corp and to the lab again" wait_for 10 both_http
    end_checks MT-02
    mt_teardown
}

# ms between the first lines since log_mark matching $1 and $2, from systemd,
# the dispatcher or a plugin instance
GAP_LINE=' (systemd\[1\]|nm-sshuttle-dispatcher\[[0-9]+\]|nm-sshuttle-service\[[0-9]+\]): '
line_time() { date -d "$(cut -d' ' -f1-3)" +%s.%N 2> /dev/null; }  # a journal line's time, in s
journal_gap_ms() {
    local a b
    a=$(journal_since_mark | grep -E "$GAP_LINE" | grep -m1 -F -- "$1" | line_time)
    b=$(journal_since_mark | grep -E "$GAP_LINE" | grep -m1 -F -- "$2" | line_time)
    [ -n "$a" ] && [ -n "$b" ] && awk -v a="$a" -v b="$b" 'BEGIN { printf "%d", (b - a) * 1000 }'
}
s_timing() {
    begin MT-03 "several tunnels: from the dispatcher's start to the instance owning its name"
    local cold warm
    sc stop "$DISPATCHER_UNIT" > /dev/null 2>&1
    sleep 1
    log_mark
    connect_ok MT-03 "activation with the dispatcher stopped (cold)" || return
    cold=$(journal_gap_ms "Starting nm-sshuttle-dispatcher.service" "Started nm-sshuttle@$SLOT.service")
    check_le MT-03a "cold: the dispatcher's start to the instance's READY=1, well inside NM's 5 s" "$cold" 3500 ms
    nmcli connection down "$CON" > /dev/null 2>&1
    wait_for 20 con_gone
    sleep 2
    log_mark
    connect_ok MT-03b "activation with the dispatcher running (warm)" || return
    warm=$(journal_gap_ms "slot $SLOT for" "Started nm-sshuttle@$SLOT.service")
    result MT-03c INFO "warm: the dispatcher's StartUnit to the instance's READY=1: ${warm:-?} ms"
    end_checks MT-03
}

# ------------------------------------------------------------------ collect
collect() {
    note "collecting"
    local u
    raw journal-all journalctl --no-pager --since "@$START_EPOCH" -o short-precise \
        -u NetworkManager -u "$DISPATCHER_UNIT" -u "$PLUGIN_GLOB" -u 'nm-sshuttle-tunnel@*.service' \
        -u nmtest-sshd -u nmtest-dns \
        -u systemd-resolved -u firewalld -u NetworkManager-dispatcher -u dbus-broker
    raw journal-plugin journalctl --no-pager --since "@$START_EPOCH" -o short-precise -u "$DISPATCHER_UNIT" -u "$PLUGIN_GLOB"
    raw firewalld firewall-cmd --get-active-zones
    {
        echo "# nm-sshuttle test-vm report"
        echo
        echo "Run: $(date -d "@$START_EPOCH" '+%F %T') to $(date '+%T'), user $USER_NAME${ONLY:+, only $ONLY}."
        echo
        echo '```'
        cat "$REPORT/system.txt"
        echo '```'
        echo
        echo "PASS $(printf '%s\n' "${RESULTS[@]}" | grep -c '|PASS|'), FAIL $NFAIL, INFO $(printf '%s\n' "${RESULTS[@]}" | grep -c '|INFO|'), SKIP $(printf '%s\n' "${RESULTS[@]}" | grep -c '|SKIP|')"
        echo
        echo "| ID | Result | Check |"
        echo "|---|---|---|"
        for u in "${RESULTS[@]}"; do
            IFS='|' read -r id st text <<< "$u"
            text=${text//|/\\|}
            echo "| $id | $st | $text |"
        done
        if [ -s "$REPORT/details.md" ]; then
            echo
            echo "## Details of failures"
            echo
            cat "$REPORT/details.md"
        fi
        echo "Raw command output and journals are in raw/."
    } > "$REPORT/report.md"
    chmod -R a+rX "$REPORT"
    echo
    echo "report: $REPORT/report.md"
    echo "PASS $(printf '%s\n' "${RESULTS[@]}" | grep -c '|PASS|'), FAIL $NFAIL"
}

# --------------------------------------------------------------------- main
[ "$KEEP" = 1 ] || trap cleanup EXIT
preflight
cleanup > /dev/null 2>&1   # leftovers of an earlier run
mkdir -p "$REPORT/raw" "$VARDIR"
journal_follow
[ "$KEEP" = 1 ] && trap journal_stop EXIT
agent_setup
topology
create_profile "$CON"

want M2-01 && s_install
want M2-02 && s_connect
want M2-03 && s_reconnect_fw
want M2-04 && s_reconnect_nofw
want M2-05 && s_long_gap
want M2-06 && s_early_flip
want M2-07 && s_link_loss
want M2-08 && s_sigterm
want M2-09 && s_kill9
want M2-10 && s_toggle_off
want M2-11 && s_reconnect_connect
want M2-12 && s_override
want M2-13 && s_nm_restart
want M2-14 && s_stoppost
want M2-16 && s_kill_sshuttle
want M2-17 && s_flush
want M2-18 && s_link_loss_up
want M2-19 && s_link_loss_preup
want M2-20 && s_managed_yes
want M2-21 && s_stop_override
want M2-22 && s_reload_conf
want M2-23 && s_roam
want M2-24 && s_no_default_route
want M2-25 && s_lock
want M2-26 && s_park
want M2-27 && s_probe
want M2-28 && s_suspend
want M2-29 && s_jump_name
want M2-30 && s_resolv_conf
want V6-01 && s_ipv6
want V6-02 && s_ipv6_roam
want TP-01 && s_tproxy
want TP-02 && s_tproxy_health
want TP-03 && s_tproxy_nm
want MT-01 && s_multi
want MT-02 && s_nested
want MT-03 && s_timing
s_selinux
reset_state
collect
[ "$NFAIL" = 0 ]
