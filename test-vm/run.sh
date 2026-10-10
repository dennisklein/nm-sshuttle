#!/bin/bash
# SPDX-License-Identifier: MIT
#
# M2 scenario driver for the installed nm-sshuttle plugin. Run as root inside
# the throwaway Fedora 44 VM (test-vm/vm.sh does it), after test-vm/install.sh:
#
#   sudo test-vm/run.sh --user tester [--only M2-05,M2-06] [--report DIR] [--keep]
#   sudo test-vm/run.sh cleanup
#
# It builds the spike's topology (jump host sshd, DNS and web in namespaces,
# an agent key for the user), creates a profile with nmcli, and drives the
# REAL plugin (nm-sshuttle.service) through the scenarios below. A gap is made
# the way it happens in life: the jump host starts refusing connections and the
# tunnel dies; lifting the block lets the plugin's own retries (1, 2, 4 ... s)
# succeed. Nothing in the plugin is patched or timed.
#
#   M2-01 install       M2-02 connect          M2-03 reconnect, firewalld x3
#   M2-04 no firewalld  M2-05 long gap         M2-06 early "activated"
#   M2-07 link loss     M2-08 SIGTERM/restart  M2-09 kill -9
#   M2-10 off in a gap  M2-11 connect/disconnect  M2-12 user override
#   M2-13 NM restart    M2-14 ExecStopPost     M2-15 SELinux denials (always)
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
CMD=run
while [ $# -gt 0 ]; do
    case $1 in
        --user) USER_NAME=$2; shift 2 ;;
        --report) REPORT=$2; shift 2 ;;
        --only) ONLY=$2; shift 2 ;;
        --keep) KEEP=1; shift ;;
        cleanup) CMD=cleanup; shift ;;
        -h|--help) sed -n '3,26p' "$0"; exit 0 ;;
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
    /usr/lib/systemd/system/nm-sshuttle.service
    /usr/lib/systemd/system/nm-sshuttle-tunnel.service
    /usr/share/dbus-1/system.d/nm-sshuttle.conf
    /usr/share/dbus-1/system-services/org.freedesktop.NetworkManager.sshuttle.service
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
    { echo "nmcli: $ACTIVATE_OUT"; echo; plog | tail -n 40; } | detail "$1" "$2"
    echo "$ACTIVATE_OUT" | head -n 5 | sed 's/^/          /'
    plog | tail -n 8 | sed 's/^/          | /'
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
dummy_state() {  # the NM device state of a new link named nmss0 under the current configuration
    local st
    ip link add "$LINK" type dummy && ip link set "$LINK" up || return 1
    wait_for 5 have_dev_state
    st=$(dev_state)
    ip link del "$LINK"
    sleep 0.5
    echo "${st:-none}"
}
unmanaged_in_print_config() { NetworkManager --print-config | grep '^unmanaged-devices=' | grep -q "interface-name:$LINK"; }

have_dev_state() { [ -n "$(dev_state)" ]; }
traffic_ok() { http_ok "http://$WEB_IP:8080/" && http_ok "http://git.corp.test:8080/"; }
still_in_gap() { echo "state '$(con_state)', guard $(guard_present && echo present || echo absent), ifindex $(link_ifindex)"; con_is activating && guard_present && [ "$(link_ifindex)" = "$1" ]; }
bounce_signals_ok() { signal_sequence "$1" | grep -q 'StateChanged:4 StateChanged:3 *$'; }
guard_and_refused() { guard_present && refused_fast; }
stays_down() { sleep 3; echo "state '$(con_state)'"; con_gone; }
zone_con_gone() { [ -z "$(con_state "$CON_ZONE")" ]; }
sigterm_logged() { plog_has "SIGTERM during gap" && plog_has "giving up"; }
new_instance_running() { local p; p=$(plugin_pid); echo "MainPID $p, before $1"; [ -n "$p" ] && [ "$p" != 0 ] && [ "$p" != "$1" ]; }
new_instance_started() { plog | grep -o 'starting (pid [0-9]*' | grep -v "pid $1\$" | grep -q .; }
idle_exit_done() { plugin_unit_down && ! bus_owned; }
no_work_after_disconnect() { ! plog | sed -n '/Disconnect: switching off/,$p' | grep -E 'reconnect attempt [0-9]+ in|Config:|tunnel dropped'; }
tunnel_not_restarted() { ! jc -u "$TUNNEL_UNIT" --since "@$1" -o cat | grep -E -q 'Starting|Started'; }
process_gone() { ! kill -0 "$1" 2> /dev/null; }
link_not_listed() { nmcli -g DEVICE device status; ! nmcli -g DEVICE device status | grep -q "^$LINK\$"; }

s_install() {
    begin M2-01 "install: files, labels, conf.d"
    check M2-01a "all files of meson install are present" installed_files_present
    check M2-01b "SELinux labels are what policy expects (restorecon -n finds nothing to change)" labels_ok
    result M2-01b-i INFO "label of nm-sshuttle.service: $(stat -c %C /usr/lib/systemd/system/nm-sshuttle.service 2> /dev/null); of the service script: $(stat -c %C "$LIBEXEC/nm-sshuttle-service" 2> /dev/null)"
    check M2-01c "SELinux is enforcing" test "$(getenforce 2> /dev/null)" = Enforcing
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
    check M2-01f "NetworkManager --print-config lists interface-name:$LINK as unmanaged" unmanaged_in_print_config
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
    check_le M2-08c "nmss0 is removed" "$T_LINK" 2500 ms
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
    # resolved before any 'nmcli general reload dns-rc': does it keep the server on nmss0?
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
    check M2-09b "nmss0, guard and tunnel unit are gone" wait_for 15 all_clean
    check M2-09c "a new plugin instance started (NM's Disconnect activates it)" wait_for 20 new_instance_started "$oldpid"
    result M2-09j INFO "cleanup lines: $(plog | grep -F 'cleanup:' | cut -d' ' -f6- | xargs -d '\n' echo | cut -c1-300)"
    result M2-09k INFO "new instance saw: $(plog | grep -E 'Disconnect: already stopped|D-Bus call' | tail -n 3 | cut -d' ' -f6- | xargs -d '\n' echo | cut -c1-200)"
    dns_snap
    result M2-09l INFO "NM DnsManager after the kill (design expects a leaked VPN entry until NM restarts): $(xargs <<< "$DNS_SNAP")"
    # The new instance idles for 60 s after its cleanup and then exits.
    if WAIT_POLL=1 wait_for 120 plog_has "idle; exiting"; then
        result M2-09d PASS "the new instance exits at its idle timeout (log line after $((WAITED_MS / 1000)) s of waiting)"
    else
        result M2-09d FAIL "the new instance did not log 'idle; exiting' within 120 s"
        diag_state M2-09d
    fi
    check M2-09e "the plugin unit is inactive and the bus name is free after the idle exit" wait_for 10 idle_exit_done
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
    check M2-10f "the plugin still runs and owns its bus name 25 s later (idle window)" plugin_owns_bus "$pid"
    end_checks M2-10
}

s_reconnect_connect() {
    begin M2-11 "Connect right after Disconnect, within the idle window"
    connect_ok M2-11 "first activation" || return
    local pid
    pid=$(plugin_pid)
    # a: asynchronous down, then up at once: the Connect may arrive while the plugin is stopping
    nmcli --wait 0 connection down "$CON" > /dev/null 2>&1
    if connect_ok M2-11a "activation again right behind an asynchronous 'connection down'"; then
        check M2-11b "the same plugin instance served it (pid $pid)" plugin_owns_bus "$pid"
        dns_checks M2-11c
    fi
    # d: synchronous down, then up
    nmcli connection down "$CON" > /dev/null 2>&1
    wait_for 20 con_gone
    if connect_ok M2-11d "activation again right after a finished 'connection down'"; then
        check M2-11e "the same plugin instance served it (pid $pid)" plugin_owns_bus "$pid"
        dns_checks M2-11f
    fi
    check M2-11g "plugin log: no Connect was ignored" plog_has_not "Connect ignored"
    result M2-11h INFO "Connect arrived during teardown (queued): $(plog | grep -c 'Connect arrived during')"
    end_checks M2-11
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
    check_le M2-13a "nmss0 is removed after the restart was requested (plugin removes it first)" "$tl" 3000 ms
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
    # a: leftovers exist before the unit starts; whichever of the plugin's startup
    # cleanup and ExecStopPost removes them, none may remain after start + stop
    make_leftovers
    check M2-14a "the unit starts with leftovers around (nmss0, guard table, stale sshuttle table)" sc start "$PLUGIN_UNIT"
    wait_for 20 all_clean
    result M2-14b INFO "after the start the plugin's startup cleanup left: $(leftovers)"
    sc stop "$PLUGIN_UNIT"
    check M2-14c "after start + stop no nmss0, guard or stale table is left" all_clean
    # d: the leftovers appear after the startup cleanup, so only ExecStopPost can remove them
    sc start "$PLUGIN_UNIT"
    sleep 3
    make_leftovers
    sc stop "$PLUGIN_UNIT"
    check M2-14d "leftovers made while the plugin idles are removed by ExecStopPost on stop" all_clean
    end_checks M2-14
}

s_selinux() {
    SCENARIO=M2-15
    note "M2-15 SELinux denials over the whole run"
    local n out
    out=$(avc_since "$START_EPOCH" "$START_OFFSET")
    n=$(grep -c . <<< "$out")
    if [ -z "$out" ]; then
        result M2-15 PASS "no SELinux denial records during the run"
    else
        result M2-15 FAIL "$n SELinux denial records during the run (raw/avc.txt)"
        echo "$out" > "$REPORT/raw/avc.txt"
        sed -E 's/^(type=[A-Z_]+) msg=audit\([^)]*\): pid=[0-9]+ uid=[0-9]+ auid=[0-9]+ ses=[0-9]+/\1/' <<< "$out" |
            sort | uniq -c | sort -rn | detail M2-15 "SELinux denials (deduplicated)"
    fi
}

# ------------------------------------------------------------------ collect
collect() {
    note "collecting"
    local u
    raw journal-all journalctl --no-pager --since "@$START_EPOCH" -o short-precise \
        -u NetworkManager -u "$PLUGIN_UNIT" -u "$TUNNEL_UNIT" -u nmtest-sshd -u nmtest-dns \
        -u systemd-resolved -u firewalld -u NetworkManager-dispatcher -u dbus-broker
    raw journal-plugin journalctl --no-pager --since "@$START_EPOCH" -o short-precise -u "$PLUGIN_UNIT"
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
s_selinux
reset_state
collect
[ "$NFAIL" = 0 ]
