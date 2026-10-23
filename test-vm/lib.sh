#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Helpers shared by test-vm/run.sh: result bookkeeping, checks that print what
# they saw, the jump-host topology, the user's agent, resolved and NetworkManager
# DNS readers, plugin liveness, and the helpers that make and end a gap.
#
# Sourced, never run. Derived from spike/spike.sh, which cannot be sourced (it
# parses its arguments and runs the whole spike on load), so the helpers are
# copied here and trimmed.
#
# The caller sets USER_NAME, REPORT and START_EPOCH before the helpers are used.

# No pager anywhere (systemd 259 forks one on a tty); every systemd tool also
# gets --no-pager through the wrappers below.
export LC_ALL=C SYSTEMD_PAGER=cat PAGER=cat SYSTEMD_COLORS=0

sc() { systemctl --no-pager "$@"; }
jc() { journalctl --no-pager "$@"; }
bc() { busctl --system --no-pager "$@"; }
rv() { resolvectl --no-pager "$@"; }

FIXED_BUS=org.freedesktop.NetworkManager.sshuttle
LIBEXEC=/usr/libexec/nm-sshuttle
STATE_DIR=/run/nm-sshuttle
DISPATCHER_UNIT=nm-sshuttle-dispatcher.service
PLUGIN_GLOB='nm-sshuttle@*.service'   # every plugin instance
CON=nmtest-corp
CON_ZONE=nmtest-corp-zone
VARDIR=/var/lib/nmtest
SSHD_DIR=/etc/ssh/nmtest
NM_TEST_CONF=/etc/NetworkManager/conf.d/99-nmtest.conf
NM_OVERRIDE=/etc/NetworkManager/conf.d/90-nm-sshuttle.conf
NM_PACKAGED=/usr/lib/NetworkManager/conf.d/90-nm-sshuttle.conf
AUDIT_LOG=/var/log/audit/audit.log
NM_DNS_PATH=org.freedesktop.NetworkManager
NM_DNS_OBJ=/org/freedesktop/NetworkManager/DnsManager
NM_DNS_IFACE=org.freedesktop.NetworkManager.DnsManager
JUMP_IP=198.51.100.1
HOST_IP=198.51.100.2
WEB_IP=10.99.0.10
DNS_IP=10.99.0.53
# IPv6 on the same links (docs/later/ipv6.md), where the kernel has it
JUMP_IP6=2001:db8:51::1
HOST_IP6=2001:db8:51::2
WEB_IP6=2001:db8:99::10
HAVE_IPV6=no
[ -e /proc/net/if_inet6 ] && HAVE_IPV6=yes
CON6=nmtest-corp6
CONTP=nmtest-corptp   # method=tproxy (docs/later/tproxy.md)
# several tunnels (docs/later/multiple-tunnels.md): a lab network inside
# 10.99.0.0/24 with its own DNS server and zone, and an sshd for it inside
# the corporate network
CONL=nmtest-lab
CONX=nmtest-overlap
LAB_WEB=10.99.0.20
LAB_DNS=10.99.0.54
LAB_JUMP=10.99.0.40
UDP_PORT=7777         # UDP echo on $WEB_IP
LANB6_CON=nmtest-lanb6
UPLINK=""
BLOCK_TABLE=nmtest-block
LANB_CON=nmtest-lanb
LANB_IF=lb0
BANNER_PORT=2222
PARK_DROPIN=/run/systemd/system/nm-sshuttle@.service.d/50-nmtest-park.conf

# Each activation runs in a slot, and everything it creates is named after it
# (docs/later/multiple-tunnels.md). The checks look at the current slot's
# names; follow_slot moves them to the newest plugin instance.
use_slot() {  # use_slot N
    SLOT=$1
    PLUGIN_UNIT=nm-sshuttle@$1.service
    TUNNEL_UNIT=nm-sshuttle-tunnel@$1.service
    GUARD_TABLE=nm-sshuttle-guard-$1
    LINK=nmss$1
    TPROXY_MARK=$(printf '0x%x' $(( 0x4e530000 + $1 )))
    TPROXY_TABLE=$(( 20051 + $1 ))
    LISTEN_PORTS="$(( 225 + 2 * $1 )) $(( 226 + 2 * $1 ))"   # the slot's own IPv4 redirect ports
    BUS=$(cat "$STATE_DIR/$1/bus-name" 2> /dev/null)
    [ -n "$BUS" ] || BUS=$FIXED_BUS
}
use_slot 0

RESULTS=()
NFAIL=0
LOGMARK=""
WAITED_MS=0

now_ms() { echo $(( $(date +%s%N) / 1000000 )); }
contains() { case $1 in *"$2"*) return 0 ;; *) return 1 ;; esac; }
note() { echo; echo "== $*"; }
die() { echo "test-vm: $*" >&2; exit 1; }
write_file() {  # write_file PATH < content
    if ! { mkdir -p "$(dirname "$1")" && cat > "$1" && chmod 644 "$1"; }; then
        die "cannot write $1"
    fi
}

# ------------------------------------------------------------------ results
result() {  # result ID PASS|FAIL|INFO|SKIP TEXT...
    local id=$1 st=$2
    shift 2
    RESULTS+=("$id|$st|$*")
    [ "$st" = FAIL ] && NFAIL=$(( NFAIL + 1 ))
    printf '%-5s %-10s %s\n' "$st" "$id" "$*"
}
# detail ID TITLE : append stdin as an excerpt to the report's Details section
detail() {
    { echo "### $1: $2"; echo; echo '```'; tail -n "${DETAIL_LINES:-80}"; echo '```'; echo; } \
        >> "$REPORT/details.md"
}
raw() {  # raw FILE CMD... : append the command and its output to raw/FILE.txt
    local f=$REPORT/raw/$1.txt
    shift
    { echo "\$ $*"; "$@" 2>&1; echo; } >> "$f"
}

# check ID TEXT CMD... : PASS or FAIL by CMD's exit code. CMD prints what it
# saw; on FAIL that output, and the state of resolved, NM and the plugin, go to
# the console and to details.md.
check() {
    local id=$1 text=$2 out rc
    shift 2
    out=$(mktemp "$VARDIR/check.XXXXXX")
    "$@" > "$out" 2>&1
    rc=$?
    if [ "$rc" = 0 ]; then
        result "$id" PASS "$text"
    else
        result "$id" FAIL "$text"
        { printf '$'; printf ' %q' "$@"; echo "  (exit $rc)"; cat "$out"; } | detail "$id" "$text"
        sed 's/^/          /' "$out" | head -n 12
        diag_state "$id"
    fi
    rm -f "$out"
}
# check_le ID TEXT VALUE LIMIT UNIT : PASS when VALUE (a number, or empty for
# "never happened") is at most LIMIT
check_le() {
    if [ -n "$3" ] && [ "$3" -le "$4" ]; then
        result "$1" PASS "$2 ($3 $5 <= $4 $5)"
    else
        result "$1" FAIL "$2 (${3:-never} $5, limit $4 $5)"
        diag_state "$1"
    fi
}

# What a failing check prints: resolved per link, NM's DNS entries, the unit
# and link state, the plugin's journal tail.
diag_state() {
    local tmp
    tmp=$(mktemp "$VARDIR/diag.XXXXXX")
    {
        echo "--- resolved per link: dns / domain / default-route"
        rv dns 2>&1
        rv domain 2>&1
        rv default-route 2>&1
        echo "--- NM DnsManager (after nmcli general reload dns-rc)"
        dns_summary
        echo "--- connection, device, link, guard, tunnel unit, plugin unit (slot $SLOT, $BUS)"
        nmcli -f NAME,TYPE,DEVICE,STATE connection show --active 2>&1
        echo "$LINK device state: $(dev_state)  ifindex: $(link_ifindex)"
        echo "guard: $(guard_present && echo present || echo absent); tunnel: $(sc is-active "$TUNNEL_UNIT")" \
            "; plugin: $(sc is-active "$PLUGIN_UNIT") pid $(plugin_pid)"
        echo "running plugin instances: $(running_slots | xargs)"
        echo "--- plugin journal tail"
        tail -n 3000 "$JOURNAL_LOG" | grep -E "$PLUGIN_LINE" | tail -n 30
    } > "$tmp" 2>&1
    DETAIL_LINES=200 detail "$1-state" "state when it failed" < "$tmp"
    sed 's/^/          | /' "$tmp" | head -n 70
    rm -f "$tmp"
}

# ------------------------------------------------------------------ waiting
wait_for() {  # wait_for SECONDS CMD... : 0 once CMD succeeds; WAITED_MS = time taken
    local t0 end
    t0=$(now_ms)
    end=$(( t0 + $1 * 1000 ))
    shift
    while :; do
        if "$@" > /dev/null 2>&1; then
            WAITED_MS=$(( $(now_ms) - t0 ))
            return 0
        fi
        if [ "$(now_ms)" -ge "$end" ]; then
            WAITED_MS=$(( $(now_ms) - t0 ))
            return 1
        fi
        sleep "${WAIT_POLL:-0.1}"
    done
}

# ------------------------------------------------------------ user and agent
as_user() {
    setpriv --reuid="$UID_U" --regid="$GID_U" --init-groups --reset-env \
        env HOME="$HOME_U" USER="$USER_NAME" LOGNAME="$USER_NAME" \
            PATH=/usr/local/bin:/usr/bin:/bin XDG_RUNTIME_DIR="$RT_U" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=$RT_U/bus" \
            ${AGENT_SOCK:+SSH_AUTH_SOCK=$AGENT_SOCK} "$@"
}
user_manager_env() { as_user systemctl --user --no-pager show-environment 2> /dev/null | sed -n "s/^$1=//p"; }

agent_setup() {
    note "user agent"
    AGENT_SOCK=$(user_manager_env SSH_AUTH_SOCK)
    if [ -z "$AGENT_SOCK" ]; then
        AGENT_SOCK=$RT_U/nmtest-agent.sock
        as_user systemd-run --user --quiet --collect --unit=nmtest-agent \
            ssh-agent -D -a "$AGENT_SOCK" > /dev/null
        as_user systemctl --user --no-pager set-environment SSH_AUTH_SOCK="$AGENT_SOCK"
        touch "$VARDIR/started-agent"
        sleep 1
    fi
    install -d -m 700 -o "$USER_NAME" -g "$GID_U" "$HOME_U/.ssh"
    rm -f "$HOME_U"/.ssh/nmtest_auto*
    as_user ssh-keygen -q -t ed25519 -N '' -C nmtest-auto -f "$HOME_U/.ssh/nmtest_auto"
    as_user ssh-add -q "$HOME_U/.ssh/nmtest_auto" < /dev/null
    raw agent as_user ssh-add -l
}

# ----------------------------------------------------------------- topology
# host --h-j--> jump (198.51.100.1, sshd) --> internal (DNS 10.99.0.53, web 10.99.0.10)
# With IPv6 also 2001:db8:51::/64 and 2001:db8:99::/64 (web ::10). The host has
# a route to 2001:db8:99::/64 through the jump host, so that a leak would arrive.
topology() {
    note "topology: host -> jump ($JUMP_IP, sshd) -> internal (DNS $DNS_IP, web $WEB_IP)"
    local i
    ip netns add jump || die "ip netns add jump"
    ip netns add internal || die "ip netns add internal"
    # '+=' appends to the list, so the packaged nmss0 entry stays in effect
    printf '[keyfile]\nunmanaged-devices+=interface-name:h-j\n' | write_file "$NM_TEST_CONF"
    nmcli general reload conf 2> /dev/null || true
    ip link add h-j type veth peer name j-h || die "veth"
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
    if [ "$HAVE_IPV6" = yes ]; then
        ip -6 addr add $HOST_IP6/64 dev h-j nodad
        ip -n jump -6 addr add $JUMP_IP6/64 dev j-h nodad
        ip -n jump -6 addr add 2001:db8:99::1/64 dev j-i nodad
        ip -n internal -6 addr add $WEB_IP6/64 dev i-j nodad
        ip -n internal -6 route add default via 2001:db8:99::1
        ip netns exec jump sysctl -qw net.ipv6.conf.all.forwarding=1
        ip -6 route add 2001:db8:99::/64 via $JUMP_IP6
    fi

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
    if [ "$HAVE_IPV6" = yes ]; then echo "ListenAddress $JUMP_IP6" >> "$SSHD_DIR/sshd_config"; fi
    install -m 644 "$HOME_U/.ssh/nmtest_auto.pub" "$SSHD_DIR/authorized_keys/$USER_NAME"
    echo "nm-sshuttle test-vm: reached through the tunnel" > "$VARDIR/www/index.html"
    restorecon -RF "$SSHD_DIR" "$VARDIR" 2> /dev/null || true

    systemd-run --quiet --collect --unit=nmtest-sshd \
        -p NetworkNamespacePath=/run/netns/jump /usr/sbin/sshd -D -e -f "$SSHD_DIR/sshd_config"
    systemd-run --quiet --collect --unit=nmtest-dns -p SyslogIdentifier=nmtest-dns \
        -p NetworkNamespacePath=/run/netns/internal /usr/bin/python3 "$REPO/lab/dns_server.py" $DNS_IP
    systemd-run --quiet --collect --unit=nmtest-web \
        -p NetworkNamespacePath=/run/netns/internal \
        /usr/bin/python3 -m http.server --bind $WEB_IP 8080 --directory "$VARDIR/www"
    systemd-run --quiet --collect --unit=nmtest-udp \
        -p NetworkNamespacePath=/run/netns/internal \
        /usr/bin/python3 "$REPO/lab/udp_echo.py" serve $WEB_IP $UDP_PORT
    [ "$HAVE_IPV6" = yes ] && systemd-run --quiet --collect --unit=nmtest-web6 -p SyslogIdentifier=nmtest-web6 \
        -p NetworkNamespacePath=/run/netns/internal \
        /usr/bin/python3 -m http.server --bind $WEB_IP6 8080 --directory "$VARDIR/www"
    for i in $(seq 1 50); do
        ss -N jump -ltnH 'sport = :22' | grep -q . &&
            ss -N internal -ltnH 'sport = :8080' | grep -q . &&
            ss -N internal -lunH 'sport = :53' | grep -q . && break
        sleep 0.2
    done
    ssh-keyscan -T 5 -t ed25519 $JUMP_IP > "$VARDIR/known_hosts.tmp" 2> /dev/null
    as_user ssh-keygen -R $JUMP_IP > /dev/null 2>&1
    if [ "$HAVE_IPV6" = yes ]; then
        ssh-keyscan -T 5 -t ed25519 $JUMP_IP6 >> "$VARDIR/known_hosts.tmp" 2> /dev/null
        as_user ssh-keygen -R $JUMP_IP6 > /dev/null 2>&1
    fi
    cat "$VARDIR/known_hosts.tmp" >> "$HOME_U/.ssh/known_hosts"
    chown "$USER_NAME:" "$HOME_U/.ssh/known_hosts"
    raw topology systemctl --no-pager status nmtest-sshd nmtest-dns nmtest-web
    [ "$HAVE_IPV6" = yes ] && raw topology systemctl --no-pager status nmtest-web6
    return 0
}

# The jump host stops answering: tcp is reset at once (a retry fails fast),
# everything else is dropped. The plugin's guard is untouched by this table.
block_jump() {
    nft -f - <<EOF
table inet $BLOCK_TABLE {
    chain out {
        type filter hook output priority -50; policy accept;
        ip daddr $JUMP_IP meta l4proto tcp reject with tcp reset
        ip daddr $JUMP_IP drop
    }
}
EOF
}
unblock_jump() { nft delete table inet "$BLOCK_TABLE" 2> /dev/null; return 0; }

# The user's key stops working on the jump host (the next login is "Permission
# denied"), and works again.
keys_off() { mv -f "$SSHD_DIR/authorized_keys/$USER_NAME" "$VARDIR/authorized_keys.off" 2> /dev/null; return 0; }
keys_on() {
    [ -f "$VARDIR/authorized_keys.off" ] || return 0
    mv -f "$VARDIR/authorized_keys.off" "$SSHD_DIR/authorized_keys/$USER_NAME"
    restorecon -F "$SSHD_DIR/authorized_keys/$USER_NAME" 2> /dev/null
    return 0
}

# A second uplink for roaming: NM-managed veth lb0 (192.168.77.2/24, default
# route at metric 10, so it becomes NM's primary connection while up). Its
# peer sits in netns lanb. The jump host stays reachable over h-j either way.
lanb_setup() {
    ip netns add lanb 2> /dev/null
    ip link add "$LANB_IF" type veth peer name lb1 || return 1
    ip link set lb1 netns lanb
    ip -n lanb addr add 192.168.77.1/24 dev lb1
    ip -n lanb link set lb1 up
    nmcli connection delete "$LANB_CON" > /dev/null 2>&1
    nmcli connection add type ethernet ifname "$LANB_IF" con-name "$LANB_CON" \
        connection.autoconnect no ipv4.method manual ipv4.addresses 192.168.77.2/24 \
        ipv4.gateway 192.168.77.1 ipv4.route-metric 10 ipv4.ignore-auto-dns yes \
        ipv6.method disabled >> "$REPORT/raw/profiles.txt" 2>&1
}
lanb_teardown() {
    nmcli connection down "$LANB_CON" > /dev/null 2>&1
    nmcli connection delete "$LANB_CON" > /dev/null 2>&1
    ip link del "$LANB_IF" 2> /dev/null
    ip netns del lanb 2> /dev/null
    return 0
}
# The second uplink with IPv6 too (2001:db8:77::2/64), for roams that change
# only the IPv6 prefix: lanb6_setup, then lanb6_address ADDR... reapplies.
lanb6_setup() {
    lanb_setup || return 1
    nmcli connection delete "$LANB_CON" > /dev/null 2>&1
    nmcli connection add type ethernet ifname "$LANB_IF" con-name "$LANB6_CON" \
        connection.autoconnect no ipv4.method manual ipv4.addresses 192.168.77.2/24 \
        ipv4.gateway 192.168.77.1 ipv4.route-metric 10 ipv4.ignore-auto-dns yes \
        ipv6.method manual ipv6.addresses 2001:db8:77::2/64 ipv6.addr-gen-mode eui64 \
        ipv6.ignore-auto-dns yes >> "$REPORT/raw/profiles.txt" 2>&1
}
lanb6_address() {  # lanb6_address ADDR/PREFIX... : the uplink's IPv6 addresses, reapplied
    nmcli connection modify "$LANB6_CON" ipv6.addresses "$(IFS=,; echo "$*")" &&
        nmcli device reapply "$LANB_IF"
} >> "$REPORT/raw/roam6.txt" 2>&1
lanb6_teardown() {
    nmcli connection down "$LANB6_CON" > /dev/null 2>&1
    nmcli connection delete "$LANB6_CON" > /dev/null 2>&1
    lanb_teardown
}
primary_con() {
    bc get-property org.freedesktop.NetworkManager /org/freedesktop/NetworkManager \
        org.freedesktop.NetworkManager PrimaryConnection 2>&1
}
nm_general() { nmcli -g STATE,CONNECTIVITY general 2>&1; }

# A server in the internal network that speaks first, for the probe
banner_start() {
    printf '%s\n' \
        'import socket, sys' \
        's = socket.socket()' \
        's.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)' \
        's.bind((sys.argv[1], int(sys.argv[2])))' \
        's.listen(16)' \
        'while True:' \
        '    c, _ = s.accept()' \
        '    try:' \
        '        c.sendall(b"SSH-2.0-nmtest-banner\r\n")' \
        '    except OSError:' \
        '        pass' \
        '    c.close()' | write_file "$VARDIR/banner.py"
    restorecon -F "$VARDIR/banner.py" 2> /dev/null
    systemd-run --quiet --collect --unit=nmtest-banner -p NetworkNamespacePath=/run/netns/internal \
        /usr/bin/python3 -I "$VARDIR/banner.py" "$WEB_IP" "$BANNER_PORT"
    wait_for 5 sh -c "ss -N internal -ltnH 'sport = :$BANNER_PORT' | grep -q ."
}
banner_stop() { sc stop nmtest-banner > /dev/null 2>&1; return 0; }
# The sshuttle server on the jump host: the user's processes in netns jump
# that run sshuttle's assembler
remote_server_pids() {
    local p
    for p in $(ip netns pids jump 2> /dev/null); do
        [ "$(stat -c %U "/proc/$p" 2> /dev/null)" = "$USER_NAME" ] || continue
        tr '\0' ' ' < "/proc/$p/cmdline" 2> /dev/null | grep -q 'assembler' && echo "$p"
    done
}

# A logind session of the user whose LockedHint the test sets as root (the VM
# is headless: no GNOME Shell locks it). The plugin counts any locked session.
LOCK_SESSION=""
user_sessions() { loginctl --no-legend list-sessions 2> /dev/null | awk -v u="$USER_NAME" '$3 == u {print $1}' | sort; }
lock_session_start() {
    local before after
    before=$(user_sessions)
    # Without a TTY pam_systemd makes a "background" session, which logind
    # does not let lock; class "user" can
    systemd-run --quiet --collect --unit=nmtest-session -p PAMName=login -p User="$USER_NAME" \
        -p Environment=XDG_SESSION_CLASS=user /usr/bin/sleep 3600 > /dev/null 2>&1 || return 1
    for _ in $(seq 1 50); do
        after=$(user_sessions)
        LOCK_SESSION=$(comm -13 <(echo "$before") <(echo "$after") | head -n 1)
        [ -n "$LOCK_SESSION" ] && return 0
        sleep 0.1
    done
    return 1
}
lock_session_stop() { sc stop nmtest-session > /dev/null 2>&1; LOCK_SESSION=""; return 0; }
set_locked() {  # set_locked true|false
    local path
    path=$(bc call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager \
        GetSession s "$LOCK_SESSION" 2> /dev/null | sed -n 's/^o "\(.*\)"$/\1/p')
    [ -n "$path" ] || { echo "no session $LOCK_SESSION"; return 1; }
    # logind lets root or the session's owner set it
    bc call org.freedesktop.login1 "$path" org.freedesktop.login1.Session SetLockedHint b "$1" ||
        as_user busctl --system --no-pager call org.freedesktop.login1 "$path" \
            org.freedesktop.login1.Session SetLockedHint b "$1"
}
attempts_logged() { plog | grep -c 'reconnect attempt [0-9]* in'; }
inhibitor_held() { systemd-inhibit --list --no-pager 2>&1 | grep -i 'nm-sshuttle'; }

# ----------------------------------------------------------------- profiles
profile_data() {  # profile_data [EXTRA-KEY=VALUE] ; REMOTE=, SUBNETS=, DNS_SERVERS= and DOMAINS= override
    printf 'remote = %s, local-user = %s, subnets = %s, dns = split, dns-servers = %s, dns-domains = %s%s' \
        "${REMOTE:-$USER_NAME@$JUMP_IP}" "$USER_NAME" "${SUBNETS:-10.99.0.0/24}" \
        "${DNS_SERVERS:-$DNS_IP}" "${DOMAINS:-~corp.test}" "${1:+, $1}"
}
# create_profile NAME [VPN-DATA-EXTRA [NMCLI-ARGS...]]  (design §4.2)
create_profile() {
    local name=$1 extra=${2:-}
    shift $(( $# > 1 ? 2 : $# ))
    nmcli connection delete "$name" > /dev/null 2>&1
    nmcli connection add type vpn con-name "$name" vpn-type sshuttle \
        connection.autoconnect no connection.permissions "user:$USER_NAME" \
        ipv4.auto-route-ext-gw no ipv6.auto-route-ext-gw no \
        ipv4.never-default yes ipv6.never-default yes \
        vpn.persistent yes vpn.data "$(profile_data "$extra")" "$@" \
        >> "$REPORT/raw/profiles.txt" 2>&1
}

con_state() { nmcli -g GENERAL.STATE connection show "${1:-$CON}" 2> /dev/null | head -n 1; }
con_is() { [ "$(con_state)" = "$1" ]; }
con_gone() { [ -z "$(con_state)" ]; }
dev_state() { nmcli -g GENERAL.STATE device show "$LINK" 2> /dev/null | awk 'NR == 1 {print $1}'; }
link_ifindex() { cat "/sys/class/net/$LINK/ifindex" 2> /dev/null; }
link_exists() { [ -e "/sys/class/net/$LINK" ]; }
link_gone() { [ ! -e "/sys/class/net/$LINK" ]; }

ACTIVATE_OUT=""
activate() {  # activate [CONNECTION] : nmcli up; 0 when it reached activated
    local con=${1:-$CON}
    ACTIVATE_OUT=$(timeout 120 nmcli --wait 90 connection up "$con" 2>&1)
    printf '$ nmcli connection up %s\n%s\n\n' "$con" "$ACTIVATE_OUT" >> "$REPORT/raw/activate.txt"
    if contains "$ACTIVATE_OUT" "was not installed" && [ ! -f "$VARDIR/nm-restarted" ]; then
        result "${SCENARIO:-M2-00}-nm" INFO "NetworkManager had not picked up the .name file; restarting it once"
        touch "$VARDIR/nm-restarted"
        sc restart NetworkManager
        nm-online -s -q -t 60
        sleep 2
        ACTIVATE_OUT=$(timeout 120 nmcli --wait 90 connection up "$con" 2>&1)
        printf '$ nmcli connection up %s (after NM restart)\n%s\n\n' "$con" "$ACTIVATE_OUT" >> "$REPORT/raw/activate.txt"
    fi
    follow_slot
    [ "$(con_state "$con")" = activated ]
}

# ------------------------------------------------------------- plugin, logs
running_slots() {  # slots whose plugin instance is starting or running
    sc list-units --plain --no-legend --state=active,activating "$PLUGIN_GLOB" 2> /dev/null |
        sed -n 's/^nm-sshuttle@\([0-7]\)\.service .*/\1/p'
}
follow_slot() {  # follow_slot : use the newest running instance's slot, if there is one
    local n t best="" bt=-1
    for n in $(running_slots); do
        t=$(sc show -p InactiveExitTimestampMonotonic --value "nm-sshuttle@$n.service" 2> /dev/null)
        [ "${t:-0}" -gt "$bt" ] && { bt=${t:-0}; best=$n; }
    done
    [ -z "$best" ] || use_slot "$best"
}
plugin_pid() { sc show -p MainPID --value "$PLUGIN_UNIT" 2> /dev/null; }
dbus_call() { bc call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus "$@"; }
# Liveness is GetNameOwner and the PID, never 'busctl status' (run 5's R4h-p).
bus_owned() { dbus_call GetNameOwner s "$BUS" > /dev/null 2>&1; }
bus_owner_pid() {  # busctl prints 's ":1.N"' and 'u PID'
    local o p
    o=$(dbus_call GetNameOwner s "$BUS" 2> /dev/null) || return 1
    o=${o#s \"}
    o=${o%\"}
    p=$(dbus_call GetConnectionUnixProcessID s "$o" 2> /dev/null) || return 1
    echo "${p#u }"
}
plugin_owns_bus() {  # plugin_owns_bus [PID] : alive, and owns the bus name
    local pid=${1:-$(plugin_pid)}
    [ -n "$pid" ] && [ "$pid" != 0 ] && kill -0 "$pid" 2> /dev/null && [ "$(bus_owner_pid)" = "$pid" ]
}

# The checks read the journal through one journalctl -f that runs for the
# whole run: fresh reads, by unit, after a cursor or since a time, at times
# missed the plugin's newest entries, which a later read showed.
JOURNAL_LOG=$VARDIR/journal.log
JOURNAL_PID=""
PLUGIN_LINE=' nm-sshuttle-service\[[0-9]+\]: '
journal_follow() {
    journalctl --no-pager --follow --lines=0 -o short-precise > "$JOURNAL_LOG" 2> /dev/null &
    JOURNAL_PID=$!
}
journal_stop() { [ -z "$JOURNAL_PID" ] || kill "$JOURNAL_PID" 2> /dev/null; JOURNAL_PID=""; }
journal_since_mark() { tail -c "+$(( ${LOGMARK:-0} + 1 ))" "$JOURNAL_LOG" 2> /dev/null; }
# log_mark : later plog calls show the plugin's journal after this point
log_mark() {
    LOGMARK=$(stat -c %s "$JOURNAL_LOG" 2> /dev/null || echo 0)
    MARK_EPOCH=$(date +%s)
}
plog() { journal_since_mark | grep -E "$PLUGIN_LINE"; }  # every plugin instance's journal since log_mark (or since the run began)
plog_has() { plog | grep -q -F -- "$1"; }
plog_has_not() { ! plog | grep -q -F -- "$1"; }
# journald tells followers about new entries in batches, so a line shows up in
# JOURNAL_LOG a moment after it was logged. journal_catch_up : until the file
# holds what journald had before this call, which a marker of our own shows.
journal_catch_up() {
    local m="nmtest-sync-$RANDOM$RANDOM"
    echo "$m" | systemd-cat -t nmtest-sync
    wait_for 5 grep -q -F -- "$m" "$JOURNAL_LOG"
}
plog_show() {  # plog_show PATTERN : the matching lines (a check's evidence); fails without any
    journal_catch_up
    wait_for 2 plog_has "$1"
    plog | grep -F -- "$1" | tail -n 8 | grep .
}
alog() {  # the activation path since log_mark: NetworkManager on the VPN, the dispatcher, the bus
    journal_since_mark | grep -E ' (NetworkManager|nm-sshuttle-dispatcher|dbus-broker[a-z-]*)\[[0-9]+\]: ' |
        grep -i -e sshuttle -e vpn
}
# Python errors and the supervisor's own error paths, since log_mark
plog_errors() {
    local errs
    journal_catch_up
    errs=$(plog | grep -E 'unexpected error|Traceback|error while handling|D-Bus handler error')
    [ -z "$errs" ] || { echo "$errs" | head -n 10; return 1; }
}

# ---------------------------------------------------------------- DNS state
# NM caches DnsManager.Configuration until it next pushes changed DNS; "reload
# dns-rc" rebuilds the property (design §2.1). Read resolved first where both
# matter: the reload pushes DNS again.
nm_dns_json() {
    nmcli general reload dns-rc > /dev/null 2>&1
    sleep 1
    bc --json=short get-property "$NM_DNS_PATH" "$NM_DNS_OBJ" "$NM_DNS_IFACE" Configuration
}
dns_summary() {  # one line per NM DNS entry: vpn=0|1 iface=NAME|- ns=a,b
    nm_dns_json 2>&1 | python3 -I -c '
import json, sys
raw = sys.stdin.read()
try:
    entries = json.loads(raw)["data"]
except Exception as e:
    print("cannot read DnsManager.Configuration: %s: %s" % (e, raw.strip()[:200]))
    sys.exit(0)
if not entries:
    print("(no entries)")
for e in entries:
    ns = e.get("nameservers", {}).get("data", [])
    print("vpn=%d iface=%s ns=%s" % (1 if e.get("vpn", {}).get("data") else 0,
                                     e.get("interface", {}).get("data", "-"), ",".join(ns)))'
}
DNS_SNAP=""
dns_snap() { DNS_SNAP=$(dns_summary); }
dns_one_vpn_entry() {
    echo "$DNS_SNAP"
    [ "$(grep -c '^vpn=1 ' <<< "$DNS_SNAP")" = 1 ] &&
        grep -q "^vpn=1 iface=$LINK ns=[^ ]*$DNS_IP" <<< "$DNS_SNAP"
}
dns_no_vpn_entry() { echo "$DNS_SNAP"; ! grep -q '^vpn=1 ' <<< "$DNS_SNAP"; }
dns_no_bare_entry() { echo "$DNS_SNAP"; ! grep -q 'iface=- ' <<< "$DNS_SNAP"; }
dns_no_device_entry() { echo "$DNS_SNAP"; ! grep -q "^vpn=0 iface=$LINK " <<< "$DNS_SNAP"; }

nmss0_resolved_ok() {  # the link carries the servers and the routing domain, never the default route
    local dns dom dr rc=0
    dns=$(rv dns "$LINK" 2>&1)
    dom=$(rv domain "$LINK" 2>&1)
    dr=$(rv default-route "$LINK" 2>&1)
    printf '%s\n%s\n%s\n' "$dns" "$dom" "$dr"
    contains "$dns" "$DNS_IP" || { echo "missing server $DNS_IP"; rc=1; }
    contains "$dom" "~corp.test" || { echo "missing domain ~corp.test"; rc=1; }
    [ "${dr##* }" = no ] || { echo "default-route is not no"; rc=1; }
    return "$rc"
}
uplink_resolved_ok() {  # the uplink keeps its servers and resolved's default route
    local dns dr rc=0
    dns=$(rv dns "$UPLINK" 2>&1)
    dr=$(rv default-route "$UPLINK" 2>&1)
    printf '%s\n%s\n' "$dns" "$dr"
    grep -q '[0-9]\.[0-9]' <<< "${dns#*:}" || { echo "$UPLINK has no DNS server"; rc=1; }
    [ "${dr##* }" = yes ] || { echo "$UPLINK default-route is not yes"; rc=1; }
    return "$rc"
}
# servers_log NAME : what the test server NAME logged since log_mark (or since the run began)
servers_log() { journal_since_mark | grep -F " $1["; }
dnslog_has() { servers_log nmtest-dns | grep -q -F -- "$1"; }
split_lookup_ok() {  # a fresh split name resolves, and the query reached the internal server via the jump host
    local n="u$RANDOM$RANDOM.corp.test" out
    out=$(rv query --cache=no --legend=no "$n" 2>&1)
    echo "$out"
    contains "$out" "$WEB_IP" || return 1
    wait_for 5 dnslog_has "query from 10.99.0.1: $n" || { echo "the internal DNS server never logged $n"; return 1; }
}
outside_lookup_ok() {  # a fresh outside name is not sent to the tunnel's DNS server
    local n="o$RANDOM$RANDOM.example.net" out
    out=$(rv query --cache=no --legend=no "$n" 2>&1)
    echo "$out"
    sleep 1
    ! dnslog_has "$n" || { echo "the internal DNS server saw $n"; return 1; }
}
http_ok() { [ "$(curl -g -s -m 5 -o /dev/null -w '%{http_code}' --noproxy '*' "$1")" = 200 ]; }
# web6_from ADDRESS : the IPv6 web server logged a request from ADDRESS since log_mark
web6_lines() { servers_log nmtest-web6 | sed 's/^[^]]*\]: //' | grep "^$1 "; }
web6_from() { wait_for 3 web6_lines "$1"; web6_lines "$1"; }

# info_or_check ID TEXT CMD... : like check, but only INFO with what CMD saw
# when INFO_ONLY lists ID's last letter (an answer, not an assertion)
info_or_check() {
    local id=$1 text=$2 out rc
    shift 2
    if contains " ${INFO_ONLY:-} " " ${id: -1} "; then
        out=$("$@" 2>&1)
        rc=$?
        result "$id" INFO "$text: $([ "$rc" = 0 ] && echo yes || echo no); saw: $(tr -s '\n' ' ' <<< "$out" | cut -c1-200)"
    else
        check "$id" "$text" "$@"
    fi
}

# dns_checks PREFIX : everything the design's checklist asks for after a
# (re)configuration: resolved per link, lookups, then NM's DnsManager.
# INFO_ONLY="b g" turns those checks into INFO (no uplink default route; a
# managed nmss0, where risk 11 is expected).
dns_checks() {
    local p=$1
    check "$p-a" "resolved: $LINK has $DNS_IP and ~corp.test, default-route no" nmss0_resolved_ok
    info_or_check "$p-b" "resolved: $UPLINK keeps its servers and the default route" uplink_resolved_ok
    check "$p-c" "a split name resolves through the tunnel" split_lookup_ok
    check "$p-d" "an outside name is not sent to the tunnel's DNS server" outside_lookup_ok
    dns_snap
    check "$p-e" "NM DnsManager: exactly one VPN entry, on $LINK, with $DNS_IP" dns_one_vpn_entry
    check "$p-f" "NM DnsManager: no entry without an interface" dns_no_bare_entry
    info_or_check "$p-g" "NM DnsManager: no non-VPN $LINK entry (risk 11)" dns_no_device_entry
}

# --------------------------------------------------------------- guard etc.
guard_present() { nft list table inet "$GUARD_TABLE" > /dev/null 2>&1; }
guard_gone() { ! guard_present; }
sshuttle_tables() { nft list tables 2> /dev/null | awk '$3 ~ /^sshuttle-ipv[46]-[0-9]+$/ {print $3}'; }
slot_listens() {  # sshuttle listens on one of the slot's own ports, as root, and has its table
    local p line
    for p in $LISTEN_PORTS; do
        # ss -e prints uid: only for a user other than root
        line=$(ss -ltnepH "src 127.0.0.1 and sport = :$p")
        grep -q '"sshuttle\|"python' <<< "$line" && ! grep -q ' uid:' <<< "$line" &&
            sshuttle_tables | grep -qx "sshuttle-ipv4-$p" && { echo "port $p"; return 0; }
    done
    echo "no root listener on $LISTEN_PORTS; tables: $(sshuttle_tables | xargs)"
    return 1
}
# the redirect ports 225-240: those listened on, and those in TIME_WAIT (design §4.8)
redirect_listens() { ss -ltnH 'src 127.0.0.1 and ( sport >= :225 and sport <= :240 )' | awk '{print $4}' | xargs; }
redirect_waits() { ss -tanH state time-wait '( sport >= :225 and sport <= :240 )' | awk '{print $3}' | sort | uniq -c | xargs; }
# hold_conn SECONDS : a TCP connection through the tunnel to the web server, held open in the background
hold_conn() {
    as_user python3 -c 'import socket, sys, time
s = socket.create_connection((sys.argv[1], 8080), timeout=5)
time.sleep(float(sys.argv[2]))' "$WEB_IP" "$1" > /dev/null 2>&1 &
}
tunnel_state() { sc is-active "$TUNNEL_UNIT" 2> /dev/null; }
# design §4.3: sshuttle has 127.0.0.1 as its remote, which it never looks up,
# and the user's ssh the profile's remote, from the bridge
bridge_remote() {
    local pid args ssh
    pid=$(sc show -p MainPID --value "$TUNNEL_UNIT" 2> /dev/null)
    args=" $(tr '\0' ' ' < "/proc/$pid/cmdline" 2> /dev/null)"
    ssh=$(pgrep -a -u "$USER_NAME" -x ssh)
    echo "sshuttle:$args"
    echo "ssh: $ssh"
    grep -q -F -- " -r 127.0.0.1 " <<< "$args" &&
        grep -q -F -- " --remote ${REMOTE:-$USER_NAME@$JUMP_IP} " <<< "$args" &&
        grep -q -F -- " ${REMOTE:-$USER_NAME@$JUMP_IP} -- " <<< "$ssh"
}
# design §4.7, §4.8: /etc is read-only in the tunnel unit, so that sshuttle's
# root firewall helper cannot write /etc/hosts
tunnel_etc_read_only() {
    local pid out
    pid=$(sc show -p MainPID --value "$TUNNEL_UNIT" 2> /dev/null)
    [ -n "$pid" ] && [ "$pid" != 0 ] || { echo "the tunnel unit has no main process"; return 1; }
    out=$(LC_ALL=C nsenter -t "$pid" -m -- touch /etc/.nm-sshuttle-probe 2>&1)
    rm -f /etc/.nm-sshuttle-probe
    echo "touch in sshuttle's mount namespace: ${out:-succeeded}"
    grep -q "Read-only file system" <<< "$out"
}
# method=tproxy: sshuttle's chains in iptables' mangle table, the plugin's rule and route
mangle_chains() { iptables -w -t mangle -S 2> /dev/null | awk '$1 == "-N" && $2 ~ /^sshuttle-[mtd]-[0-9]+$/ {print $2}'; }
tproxy_rule() { ip rule list 2> /dev/null | grep "fwmark $TPROXY_MARK lookup $TPROXY_TABLE"; }
tproxy_route() { ip route show table $TPROXY_TABLE 2> /dev/null | grep '^local default dev lo'; }
tunnel_active() { [ "$(tunnel_state)" = active ]; }
tunnel_gone() { case $(tunnel_state) in inactive|failed|unknown) return 0 ;; *) return 1 ;; esac; }
# In every slot: links, guard tables, running tunnel units
any_links() { ip -o link show 2> /dev/null | awk -F': ' '$2 ~ /^nmss[0-7](@|$)/ {sub(/@.*/, "", $2); print $2}'; }
any_guards() { nft list tables 2> /dev/null | awk '$3 ~ /^nm-sshuttle-guard/ {print $3}'; }
any_tunnels() { sc list-units --plain --no-legend --state=active,activating,deactivating 'nm-sshuttle-tunnel@*.service' 2> /dev/null | awk '{print $1}'; }
any_tproxy_rules() { ip rule list 2> /dev/null | grep -E 'fwmark 0x4e53000[0-7] lookup 2005[1-8]'; }
all_clean() {  # in no slot a link, a guard or a tunnel unit; no stale sshuttle table
    echo "links: '$(any_links | xargs)'; guards: '$(any_guards | xargs)';" \
        "tunnels: '$(any_tunnels | xargs)'; sshuttle tables: '$(sshuttle_tables | xargs)'"
    [ -z "$(any_links)$(any_guards)$(any_tunnels)$(sshuttle_tables)" ]
}
fw_running() { command -v firewall-cmd > /dev/null && sc is-active --quiet firewalld; }
fw_zone_of_link() { firewall-cmd --get-zone-of-interface="$LINK" 2>&1; }
fw_bound() { firewall-cmd --get-zone-of-interface="$LINK" > /dev/null 2>&1; }
fw_default_zone() { firewall-cmd --get-default-zone 2>&1; }

# ---------------------------------------------------------- gaps and signals
# A gap, naturally: the jump host refuses us, then the tunnel dies. The plugin
# notices the unit stopping, sends STARTING, and retries at 1, 2, 4 ... s; every
# retry fails until the block is lifted.
drop_tunnel() { sc kill --kill-whom=main -s KILL "$TUNNEL_UNIT"; }
gap_begin() {
    block_jump || return 1
    drop_tunnel || { echo "cannot kill $TUNNEL_UNIT"; return 1; }
    wait_for 15 con_is activating || { echo "NM state after the drop: '$(con_state)'"; return 1; }
}
gap_end() { unblock_jump; }
# refused fast: a connect to the tunnelled subnet fails at once (the guard's reject)
refused_fast() {
    local t0 out rc
    t0=$(now_ms)
    out=$(curl -s -m 5 -o /dev/null -w '%{http_code}' --noproxy '*' "http://$WEB_IP:8080/" 2>&1)
    rc=$?
    echo "curl exit $rc, http '$out', $(( $(now_ms) - t0 )) ms"
    [ "$rc" = 7 ] && [ $(( $(now_ms) - t0 )) -lt 3000 ]
}
gap_checks() {  # gap_checks PREFIX IFINDEX : what must hold while the tunnel is down
    local p=$1
    check "$p-g" "guard table present during the gap" guard_present
    check "$p-l" "$LINK keeps its ifindex during the gap" test "$(link_ifindex)" = "$2"
    check "$p-r" "traffic to the subnets is refused at once during the gap (fail closed)" refused_fast
    check "$p-t" "the tunnel unit is not active during the gap" test "$(tunnel_state)" != active
}
# recovery_checks PREFIX IFINDEX : activated again, same link, DNS, tunnel, traffic
recovery_checks() {
    local p=$1
    if WAIT_POLL=0.5 wait_for 150 con_is activated; then
        result "$p" PASS "activated again after the gap ($((WAITED_MS / 1000)) s after the block was lifted)"
    else
        result "$p" FAIL "not activated again within 150 s (state '$(con_state)')"
        plog | tail -n 30 | detail "$p" "plugin journal"
        diag_state "$p"
        return 1
    fi
    sleep 1
    check "$p-i" "same ifindex ($2) after the reconnect" test "$(link_ifindex)" = "$2"
    info_or_check "$p-y" "$LINK is unmanaged (device state 10) after the reconnect" test "$(dev_state)" = 10
    check "$p-u" "tunnel unit active and the guard present again" sh -c \
        "[ \"\$(systemctl --no-pager is-active $TUNNEL_UNIT)\" = active ] && nft list table inet $GUARD_TABLE > /dev/null"
    info_or_check "$p-w" "traffic flows through the tunnel again" http_ok "http://$WEB_IP:8080/"
    dns_checks "$p"
}

SIGMON_PID=""
# By interface, not by sender: an activation's bus name is known only once it runs.
SIGMATCH="type='signal',interface='org.freedesktop.NetworkManager.VPN.Plugin'"
signals_start() {  # signals_start FILE : record the plugin's D-Bus signals
    if command -v stdbuf > /dev/null; then
        stdbuf -oL busctl --system --no-pager monitor --match "$SIGMATCH" > "$1" 2>&1 &
    else
        busctl --system --no-pager monitor --match "$SIGMATCH" > "$1" 2>&1 &
    fi
    SIGMON_PID=$!
    sleep 0.5
}
signals_stop() { sleep 0.5; kill "$SIGMON_PID" 2> /dev/null; wait "$SIGMON_PID" 2> /dev/null; SIGMON_PID=""; }
signal_sequence() {  # "StateChanged:3 Config Ip4Config Failure:1 StateChanged:6": the plugin's signals in order
    awk '/Interface=org.freedesktop.NetworkManager.VPN.Plugin +Member=/ {
            m = $0; sub(/.*Member=/, "", m); sub(/[ \t].*/, "", m)
            if (m == "StateChanged" || m == "Failure") { cur = m; want = 1 } else { printf "%s ", m }
            next }
        want && /UINT32/ { v = $2; sub(/;/, "", v); printf "%s:%s ", cur, v; want = 0 }' "$1"
}

plugin_unit_down() { case $(sc is-active "$PLUGIN_UNIT") in inactive|failed) return 0 ;; *) return 1 ;; esac; }
not_bus_owned() { ! bus_owned; }

# watch_teardown LIMIT_S : poll until con, link, guard, tunnel and plugin unit
# are all gone; T_* = milliseconds since the call (empty = not seen)
T_CON="" T_LINK="" T_GUARD="" T_TUNNEL="" T_UNIT=""
watch_teardown() {
    local t0 end
    t0=$(now_ms)
    end=$(( t0 + $1 * 1000 ))
    T_CON="" T_LINK="" T_GUARD="" T_TUNNEL="" T_UNIT=""
    while [ "$(now_ms)" -lt "$end" ]; do
        [ -n "$T_CON" ] || { con_gone && T_CON=$(( $(now_ms) - t0 )); }
        [ -n "$T_LINK" ] || { link_gone && T_LINK=$(( $(now_ms) - t0 )); }
        [ -n "$T_GUARD" ] || { guard_gone && T_GUARD=$(( $(now_ms) - t0 )); }
        [ -n "$T_TUNNEL" ] || { tunnel_gone && T_TUNNEL=$(( $(now_ms) - t0 )); }
        [ -n "$T_UNIT" ] || { plugin_unit_down && T_UNIT=$(( $(now_ms) - t0 )); }
        [ -n "$T_CON" ] && [ -n "$T_LINK" ] && [ -n "$T_GUARD" ] && [ -n "$T_TUNNEL" ] && [ -n "$T_UNIT" ] && break
        sleep 0.05
    done
}

# ------------------------------------------------------------ reset, cleanup
# Between scenarios: VPN down, plugin stopped, every leftover removed.
leftovers() {
    local l="" x
    for x in $(any_links) $(any_guards) $(any_tunnels); do l="$l $x"; done
    [ -z "$(sshuttle_tables)" ] || l="$l sshuttle-table"
    [ -z "$(mangle_chains)" ] || l="$l sshuttle-mangle-chain"
    [ -z "$(any_tproxy_rules)" ] || l="$l tproxy-routing"
    echo "${l# }"
}
remove_all() {  # stop every plugin instance and tunnel unit and remove what they left, in all slots
    local x
    sc stop "$PLUGIN_GLOB" 'nm-sshuttle-tunnel@*.service' > /dev/null 2>&1
    sc reset-failed "$PLUGIN_GLOB" 'nm-sshuttle-tunnel@*.service' > /dev/null 2>&1
    for x in $(any_links); do
        fw_running && firewall-cmd --remove-interface="$x" > /dev/null 2>&1
        ip link del "$x" 2> /dev/null
    done
    for x in $(any_guards); do nft delete table inet "$x" 2> /dev/null; done
    for x in $(sshuttle_tables); do nft delete table inet "$x"; done
    while read -r x; do
        # shellcheck disable=SC2086  # the rule's words
        [ -n "$x" ] && ip rule del ${x#*:} 2> /dev/null
    done <<< "$(any_tproxy_rules)"
    rm -f "$STATE_DIR"/[0-7]/claim.json
    use_slot 0
}
clear_leaked_dns() {  # a leaked VPN DNS entry would spoil later DNS checks
    dns_snap
    if grep -q '^vpn=1 ' <<< "$DNS_SNAP"; then
        result "${SCENARIO:-M2-00}-z" INFO "restarting NetworkManager to clear a leftover VPN DNS entry: $(grep '^vpn=1 ' <<< "$DNS_SNAP" | xargs)"
        sc restart NetworkManager
        nm-online -s -q -t 60
        sleep 2
    fi
}
reset_state() {
    local c left
    unblock_jump
    for c in "$CON" "$CON_ZONE" "$CON6" "$CONTP" "$CONL" "$CONX"; do
        [ -n "$(con_state "$c")" ] && nmcli connection down "$c" > /dev/null 2>&1
    done
    wait_for 30 con_gone
    sleep 1
    left=$(leftovers)
    [ -z "$left" ] || result "${SCENARIO:-M2-00}-L" INFO "left over from the previous scenario, now removed: $left"
    remove_all
    return 0
}

audit_offset() { stat -c %s "$AUDIT_LOG" 2> /dev/null || echo -1; }
avc_since() {  # avc_since EPOCH OFFSET : SELinux denial records since then
    if [ "$2" -ge 0 ] && [ -r "$AUDIT_LOG" ]; then
        tail -c +"$(( $2 + 1 ))" "$AUDIT_LOG" | grep -E '^type=(AVC|USER_AVC|SELINUX_ERR)'
    else
        jc --since "@$1" -o cat 2> /dev/null | grep -E 'avc: +denied|SELINUX_ERR'
    fi
}

cleanup() {
    set +e
    note "cleaning up"
    journal_stop
    unblock_jump
    nmcli connection down "$CON" > /dev/null 2>&1
    nmcli connection down "$CON_ZONE" > /dev/null 2>&1
    nmcli connection down "$CON6" > /dev/null 2>&1
    nmcli connection down "$CONTP" > /dev/null 2>&1
    nmcli connection down "$CONL" > /dev/null 2>&1
    nmcli connection delete "$CON" "$CON_ZONE" "$CON6" "$CONTP" "$CONL" "$CONX" > /dev/null 2>&1
    remove_all
    sc stop nmtest-sshd nmtest-dns nmtest-web nmtest-web6 nmtest-dns-local nmtest-udp \
        nmtest-sshd2 nmtest-dns2 nmtest-web2 > /dev/null 2>&1
    rv revert h-j > /dev/null 2>&1
    if [ -f "$VARDIR/ssh-config-written" ]; then
        rm -f "$HOME_U/.ssh/config" "$VARDIR/ssh-config-written"
    fi
    if [ -f "$VARDIR/packaged-conf.bak" ]; then
        mv -f "$VARDIR/packaged-conf.bak" "$NM_PACKAGED"
    fi
    [ -f "$VARDIR/override-written" ] && rm -f "$NM_OVERRIDE"
    rm -f "$NM_TEST_CONF"
    nmcli general reload conf > /dev/null 2>&1
    [ -f "$VARDIR/firewalld-stopped" ] && sc start firewalld
    local t
    ip link del h-j 2> /dev/null
    keys_on
    if [ -f "$VARDIR/never-default" ] && [ -n "$UPLINK" ]; then
        nmcli device modify "$UPLINK" ipv4.never-default no > /dev/null 2>&1
    fi
    lanb6_teardown
    banner_stop
    lock_session_stop
    if [ -f "$PARK_DROPIN" ]; then rm -f "$PARK_DROPIN"; sc daemon-reload; fi
    for t in jump internal; do ip netns del "$t" 2> /dev/null; done
    if [ -d "$HOME_U/.ssh" ]; then
        AGENT_SOCK=${AGENT_SOCK:-$(user_manager_env SSH_AUTH_SOCK)}
        for t in "$HOME_U"/.ssh/nmtest_auto*.pub; do
            [ -e "$t" ] && as_user ssh-add -d "$t" > /dev/null 2>&1
        done
        rm -f "$HOME_U"/.ssh/nmtest_auto*
        as_user ssh-keygen -R "$JUMP_IP" > /dev/null 2>&1
        as_user ssh-keygen -R "$JUMP_IP6" > /dev/null 2>&1
        as_user ssh-keygen -R "$LAB_JUMP" > /dev/null 2>&1
    fi
    if [ -f "$VARDIR/started-agent" ]; then
        as_user systemctl --user --no-pager stop nmtest-agent 2> /dev/null
        as_user systemctl --user --no-pager unset-environment SSH_AUTH_SOCK 2> /dev/null
    fi
    rm -rf "$SSHD_DIR" "$VARDIR"
    echo "done"
}
